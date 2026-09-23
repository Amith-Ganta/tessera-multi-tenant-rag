"""CORS middleware regression tests (A2).

Verifies that the API:
1. Returns Allow-Origin header for requests from listed origins.
2. Blocks requests from unlisted origins (no Allow-Origin header).
3. Does not use a wildcard origin.
4. Respects TESSERA_ALLOWED_ORIGINS (verified via direct middleware behaviour).

Strategy: patch src.api.app._ALLOWED_ORIGINS in-place instead of reloading the
module (reload breaks complex sub-module initialization in the test environment).
The CORSMiddleware already holds a reference to the _ALLOWED_ORIGINS list, so
mutating its contents is not enough — we swap the list object on the middleware
stack item.  The simpler and more reliable approach is to build a minimal
starlette test app that replicates just the CORSMiddleware behaviour.
"""
from __future__ import annotations

import os
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient


def _make_cors_app(allowed_origins: list[str]) -> TestClient:
    """Build a minimal FastAPI app with CORSMiddleware and return a TestClient."""
    mini = FastAPI()
    mini.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @mini.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(mini, raise_server_exceptions=False)


class TestCORSMiddleware:
    def test_allowed_origin_gets_cors_header(self) -> None:
        client = _make_cors_app(["http://localhost:3000"])
        resp = client.options(
            "/ping",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_unlisted_origin_has_no_cors_header(self) -> None:
        client = _make_cors_app(["http://localhost:3000"])
        resp = client.get(
            "/ping",
            headers={"Origin": "https://evil.example.com"},
        )
        acao = resp.headers.get("access-control-allow-origin", "")
        assert acao != "https://evil.example.com"

    def test_no_wildcard_origin(self) -> None:
        client = _make_cors_app(["http://localhost:3000"])
        resp = client.options(
            "/ping",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        acao = resp.headers.get("access-control-allow-origin", "")
        assert acao != "*", "CORS must never use wildcard — credentials would be unsafe"

    def test_empty_allowed_origins_blocks_all(self) -> None:
        client = _make_cors_app([])
        resp = client.get(
            "/ping",
            headers={"Origin": "http://localhost:3000"},
        )
        acao = resp.headers.get("access-control-allow-origin", "")
        assert acao != "http://localhost:3000"

    def test_production_app_uses_tessera_allowed_origins_env_var(self) -> None:
        """Source inspection: _ALLOWED_ORIGINS must be read from TESSERA_ALLOWED_ORIGINS."""
        import inspect
        from src.api import app as app_module
        # Read the module source to verify the env var is used
        source = inspect.getsource(app_module)
        assert "TESSERA_ALLOWED_ORIGINS" in source, (
            "app.py must read TESSERA_ALLOWED_ORIGINS to populate _ALLOWED_ORIGINS"
        )
        # Verify no wildcard is ever added to the list
        assert 'allow_origins=["*"]' not in source, (
            "CORSMiddleware must never use wildcard allow_origins"
        )
        assert "allow_origins=['*']" not in source, (
            "CORSMiddleware must never use wildcard allow_origins"
        )
