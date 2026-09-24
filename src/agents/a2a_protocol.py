"""Official Google A2A protocol helpers (AgentCard discovery + JSON-RPC 2.0).

The Agent-to-Agent (A2A) protocol is defined by two wire primitives:

1. **AgentCard discovery** at ``GET /.well-known/agent.json`` — a JSON document
   describing the agent's identity, capabilities, and skills.
2. **JSON-RPC 2.0** at ``POST /`` — clients submit tasks with the
   ``message/send`` method and receive a task result with zero or more artifacts.

This module implements those contracts directly so the Drafter and Judge agents
interoperate with any standards-compliant A2A client while running on the
project's existing (frozen) dependency set. Structured skill parameters ride in
``message.metadata``; for clients that only send text parts, a JSON object in the
first text part is accepted as an equivalent fallback.

An ``a2a-sdk`` dependency is declared in ``pyproject.toml``/``requirements.txt``
for teams that prefer the SDK client, but the wire format used here is the spec
itself, so nothing in this module requires the SDK at import time.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.auth.service_auth import SERVICE_AUTH_HEADER, verify_service_token

_log = logging.getLogger(__name__)


def build_agent_card(
    name: str,
    description: str,
    url: str,
    skill_id: str,
    skill_name: str,
    skill_description: str,
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Build an A2A AgentCard document."""
    return {
        "name": name,
        "description": description,
        "url": url,
        "version": version,
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": skill_id,
                "name": skill_name,
                "description": skill_description,
                "tags": ["rag", "tessera", "compliance"],
                "inputModes": ["text"],
                "outputModes": ["text"],
            }
        ],
    }


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> JSONResponse:
    # JSON-RPC errors are HTTP 200 with an "error" object in the body.
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        },
    )


def _extract_metadata(body: dict) -> dict[str, Any]:
    """Extract skill parameters from a ``message/send`` request body.

    Parameters are read from ``params.message.metadata``. If that is empty, a
    JSON object in the first text part is parsed as a fallback so simple A2A
    clients that only emit text parts still work.
    """
    params = body.get("params") or {}
    message = params.get("message") or {}
    metadata = message.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    if not metadata:
        parts = message.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("kind") == "text" or "text" in part:
                text = part.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        metadata = parsed
                except (json.JSONDecodeError, TypeError):
                    pass
                break

    return {str(key): value for key, value in metadata.items()}


def build_task_result(skill_id: str, payload: dict) -> dict[str, Any]:
    """Wrap a skill's return dict into an A2A completed task with an artifact."""
    task_id = uuid.uuid4().hex
    serialized = json.dumps(payload, default=str)
    return {
        "id": task_id,
        "contextId": uuid.uuid4().hex,
        "kind": "task",
        "status": {
            "state": "completed",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": ""}]},
        },
        "artifacts": [
            {
                "artifactId": f"{task_id}-artifact",
                "name": skill_id,
                "parts": [
                    {"kind": "data", "data": payload},
                    {"kind": "text", "text": serialized},
                ],
            }
        ],
    }


def make_a2a_app(
    name: str,
    description: str,
    url: str,
    skill_id: str,
    skill_name: str,
    skill_description: str,
    handler: Callable[[dict], dict],
    authenticate: bool = True,
) -> FastAPI:
    """Build a FastAPI A2A server exposing AgentCard + JSON-RPC ``message/send``.

    ``handler`` is a synchronous callable that receives the skill parameters
    (from ``message.metadata``) and returns the skill result dict. It runs in a
    threadpool so a slow generation never blocks the event loop.

    When ``authenticate=True`` (the default) the agent requires the caller to
    supply a valid ``X-Tessera-Service-Token`` header.  The token is read from
    the ``TESSERA_A2A_SERVICE_TOKEN`` environment variable at request time.
    When that env var is not set the check is skipped (dev / test mode) and a
    warning is emitted.  Pass ``authenticate=False`` only in unit tests that
    deliberately test the unauthenticated code path.
    """
    from src.auth.service_auth import get_service_token as _get_token

    app = FastAPI(title=name, version="1.0.0")
    card = build_agent_card(
        name, description, url, skill_id, skill_name, skill_description
    )

    @app.get("/.well-known/agent.json")
    async def agent_card() -> dict[str, Any]:
        return card

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": name}

    @app.post("/", response_model=None)
    async def jsonrpc(request: Request) -> Any:
        # Returns either a JSON-RPC result dict or a JSON-RPC error response, so
        # no single Pydantic response model applies (response_model=None).

        if authenticate:
            token = _get_token()
            if token is not None:
                provided = request.headers.get(SERVICE_AUTH_HEADER)
                if not verify_service_token(provided):
                    _log.warning(
                        "A2A service auth failed for agent '%s' — missing or invalid %s",
                        name,
                        SERVICE_AUTH_HEADER,
                    )
                    return JSONResponse(
                        status_code=401,
                        content={"error": "service authentication required"},
                    )
            else:
                _log.warning(
                    "TESSERA_A2A_SERVICE_TOKEN not set — agent '%s' running without service auth",
                    name,
                )

        body = await request.json()
        req_id = body.get("id")
        method = body.get("method")

        if method != "message/send":
            return _jsonrpc_error(req_id, -32601, f"method not found: {method}")

        try:
            metadata = _extract_metadata(body)
            result = await run_in_threadpool(handler, metadata)
            if not isinstance(result, dict):
                result = {"result": result}
            return _jsonrpc_result(req_id, build_task_result(skill_id, result))
        except Exception as exc:  # noqa: BLE001 - an agent error is a JSON-RPC error
            return _jsonrpc_error(req_id, -32603, f"{type(exc).__name__}: {exc}")

    return app
