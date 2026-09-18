"""A2A supervisor: orchestrates Drafter -> Judge -> refine over the A2A protocol.

This replaces the in-memory custom guard loop with a resumable orchestrator that:

* calls the Drafter and Judge agents over the official A2A protocol (AgentCard
  discovery + JSON-RPC 2.0 ``message/send``) when they are reachable, and falls
  back to in-process execution of the same skill functions when they are not;
* runs the ``Drafter -> Judge -> Feedback -> Refine`` loop capped at
  ``MAX_RETRIES`` refine retries;
* persists the full state (transcript, retries, last draft, question, tenant) to
  the SQLite checkpointer before every LLM call and after every agent response,
  so a pod restart, provider failure, or long-running job can resume mid-flight;
* catches provider failures and retries from the last saved state;
* records A2A call latency, judge latency, fallback recovery time, checkpointer
  write/read latency, and end-to-end latency for the metrics report.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import requests

from src.rag.checkpointer import SQLiteCheckpointer
from src.state.redis_checkpointer import checkpointer_factory
from src.rag.config import RETRIEVER_TOP_K
from src.rag.models import DEFAULT_MODEL

MAX_RETRIES = 2

DRAFTER_URL = os.environ.get("TESSERA_DRAFTER_URL", "http://localhost:8001")
JUDGE_URL = os.environ.get("TESSERA_JUDGE_URL", "http://localhost:8002")
A2A_TIMEOUT_SECONDS = float(os.environ.get("TESSERA_A2A_TIMEOUT_SECONDS", "120"))
# "http" uses the real A2A protocol; "in_process" calls the skill functions
# directly (used for local runs and as the fallback path).
DEFAULT_MODE = os.environ.get("TESSERA_A2A_MODE", "http").strip().lower()


class A2AJsonRpcClient:
    """Minimal A2A JSON-RPC 2.0 client over HTTP."""

    def __init__(self, base_url: str, timeout: float = A2A_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send_message(self, metadata: dict) -> dict:
        """Submit ``message/send`` and return the task result object."""
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": json.dumps(metadata, default=str)}],
                    "metadata": metadata,
                }
            },
        }
        response = requests.post(f"{self.base_url}/", json=payload, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"A2A error: {body['error']}")
        return body.get("result", {})


def _extract_skill_payload(a2a_result: dict) -> dict:
    """Pull the skill's return dict out of an A2A task result's artifacts."""
    for artifact in a2a_result.get("artifacts", []):
        for part in artifact.get("parts", []):
            if not isinstance(part, dict):
                continue
            if part.get("kind") == "data":
                data = part.get("data")
                if isinstance(data, dict):
                    return data
            if part.get("kind") == "text":
                try:
                    parsed = json.loads(part.get("text", ""))
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    continue
    return {}


class A2ASupervisor:
    """Orchestrates the A2A Drafter/Judge loop with a durable checkpointer."""

    def __init__(
        self,
        drafter_url: str = DRAFTER_URL,
        judge_url: str = JUDGE_URL,
        checkpointer: SQLiteCheckpointer | None = None,
        model: str = DEFAULT_MODEL,
        top_k: int = RETRIEVER_TOP_K,
        mode: str | None = None,
    ) -> None:
        self.drafter_url = drafter_url
        self.judge_url = judge_url
        self.checkpointer = checkpointer or checkpointer_factory()
        self.model = model
        self.top_k = top_k
        self.mode = (mode or DEFAULT_MODE).strip().lower()
        if self.mode not in {"http", "in_process"}:
            self.mode = "http"
        self.drafter = A2AJsonRpcClient(drafter_url)
        self.judge = A2AJsonRpcClient(judge_url)

        self.metrics: dict[str, Any] = {
            "drafter_latency_sec": [],
            "judge_latency_sec": [],
            "checkpointer_write_latency_sec": [],
            "checkpointer_read_latency_sec": [],
            "fallback_recovery_sec": [],
            "e2e_latency_sec": [],
            "fallback_events": 0,
            "requests": 0,
            "successes": 0,
            "unverified": 0,
            "retries_used": [],
        }

    # ------------------------------------------------------------------ helpers

    def _save(self, state: dict) -> None:
        start = time.perf_counter()
        self.checkpointer.save_state(state["thread_id"], state)
        self.metrics["checkpointer_write_latency_sec"].append(time.perf_counter() - start)

    def _load(self, thread_id: str) -> dict | None:
        start = time.perf_counter()
        state = self.checkpointer.load_state(thread_id)
        self.metrics["checkpointer_read_latency_sec"].append(time.perf_counter() - start)
        return state

    def _call_drafter_http(self, question: str, tenant_slug: str, feedback: str | None, previous_draft: str | None) -> dict:
        result = self.drafter.send_message(
            {
                "question": question,
                "tenant_slug": tenant_slug,
                "feedback": feedback,
                "previous_draft": previous_draft,
                "top_k": self.top_k,
                "model": self.model,
                "force_route": "vector",
            }
        )
        return _extract_skill_payload(result)

    def _call_drafter_in_process(self, question: str, tenant_slug: str, feedback: str | None, previous_draft: str | None) -> dict:
        from src.agents.drafter_agent import draft_answer

        return draft_answer(
            question=question,
            tenant_slug=tenant_slug,
            feedback=feedback,
            previous_draft=previous_draft,
            top_k=self.top_k,
            model=self.model,
            force_route="vector",
        )

    def _call_judge_http(self, question: str, draft: str, context: list, tenant_slug: str) -> dict:
        result = self.judge.send_message(
            {
                "draft": draft,
                "context": context,
                "tenant_slug": tenant_slug,
                "question": question,
            }
        )
        return _extract_skill_payload(result)

    def _call_judge_in_process(self, question: str, draft: str, context: list, tenant_slug: str) -> dict:
        from src.agents.judge_agent import judge_answer

        return judge_answer(
            draft=draft,
            context=context,
            tenant_slug=tenant_slug,
            question=question,
        )

    def _call_drafter(self, question: str, tenant_slug: str, feedback: str | None, previous_draft: str | None, transcript: list) -> tuple[dict, str]:
        """Call the Drafter; on HTTP failure fall back to in-process and time it."""
        start = time.perf_counter()
        if self.mode == "http":
            try:
                payload = self._call_drafter_http(question, tenant_slug, feedback, previous_draft)
                self.metrics["drafter_latency_sec"].append(time.perf_counter() - start)
                return payload, "http"
            except Exception as exc:
                self.metrics["fallback_events"] += 1
                transcript.append(
                    {
                        "role": "supervisor",
                        "action": "drafter_fallback",
                        "error": f"{type(exc).__name__}: {exc}",
                        "detail": "Drafter unreachable; recovering in-process.",
                    }
                )
                recovery_start = time.perf_counter()
                payload = self._call_drafter_in_process(question, tenant_slug, feedback, previous_draft)
                recovery = time.perf_counter() - recovery_start
                self.metrics["fallback_recovery_sec"].append(recovery)
                self.metrics["drafter_latency_sec"].append(time.perf_counter() - start)
                self.mode = "in_process"
                return payload, "in_process"

        payload = self._call_drafter_in_process(question, tenant_slug, feedback, previous_draft)
        self.metrics["drafter_latency_sec"].append(time.perf_counter() - start)
        return payload, self.mode

    def _call_judge(self, question: str, draft: str, context: list, tenant_slug: str) -> dict | None:
        """Call the Judge; return None when the judge is unavailable (never gates)."""
        start = time.perf_counter()
        try:
            if self.mode == "http":
                payload = self._call_judge_http(question, draft, context, tenant_slug)
            else:
                payload = self._call_judge_in_process(question, draft, context, tenant_slug)
        except Exception as exc:  # noqa: BLE001 - judge outage must not 500 /ask
            self.metrics["judge_latency_sec"].append(time.perf_counter() - start)
            return {"score": {}, "passed": None, "feedback": None, "enabled": False, "error": f"{type(exc).__name__}: {exc}"}
        self.metrics["judge_latency_sec"].append(time.perf_counter() - start)
        return payload

    # --------------------------------------------------------------- main flow

    def process_question(
        self,
        question: str,
        tenant_slug: str,
        thread_id: str | None = None,
        *,
        run_eval: bool = True,
        expected_output: str | None = None,
    ) -> dict:
        """Run the A2A Drafter -> Judge -> refine loop with checkpointing."""
        e2e_start = time.perf_counter()
        thread_id = thread_id or uuid.uuid4().hex

        # Resume any prior state for this thread (pod restart / mid-flight crash).
        prior = self._load(thread_id)
        transcript = list((prior or {}).get("transcript", []))
        retries_used = int((prior or {}).get("retries", 0))
        last_draft = (prior or {}).get("last_draft") or ""

        feedback: str | None = None
        final_answer = last_draft or ""
        final_contexts: list[str] = []
        final_sources: list[str] = []
        final_route = ""
        guard_passed: bool | None = None
        attempts_used = 0
        note = ""
        served_by = "http"

        max_attempts = MAX_RETRIES + 1  # 3 total drafts (1 initial + 2 refines)

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            state = {
                "thread_id": thread_id,
                "transcript": transcript,
                "retries": retries_used,
                "last_draft": final_answer,
                "current_question": question,
                "tenant_slug": tenant_slug,
                "created_at": (prior or {}).get("created_at", time.time()),
            }
            # Save before the LLM call so a crash here resumes at this step.
            self._save(state)

            previous_draft = final_answer if retries_used > 0 else None
            draft_payload, served_by = self._call_drafter(
                question, tenant_slug, feedback, previous_draft, transcript
            )
            final_answer = draft_payload.get("draft", "")
            final_contexts = list(draft_payload.get("context", []) or [])
            final_sources = list(draft_payload.get("sources", []) or [])
            final_route = draft_payload.get("route", "")
            transcript.append(
                {
                    "role": "drafter",
                    "action": "draft",
                    "attempt": attempt,
                    "draft": final_answer,
                    "context_count": len(final_contexts),
                    "transport": served_by,
                }
            )
            state["last_draft"] = final_answer
            state["transcript"] = transcript
            state["retries"] = retries_used
            self._save(state)

            if not run_eval:
                note = "eval disabled, single un-guarded draft"
                guard_passed = None
                transcript.append({"role": "supervisor", "action": "skip_judge", "note": note})
                break

            judge = self._call_judge(question, final_answer, final_contexts, tenant_slug)
            judge_score = judge.get("score", {}) if judge else {}
            judge_passed = judge.get("passed") if judge else None
            judge_feedback = judge.get("feedback") if judge else None
            judge_enabled = bool((judge or {}).get("enabled", False))
            transcript.append(
                {
                    "role": "judge",
                    "action": "judge",
                    "attempt": attempt,
                    "score": judge_score,
                    "passed": judge_passed,
                    "feedback": judge_feedback,
                    "enabled": judge_enabled,
                }
            )
            state["transcript"] = transcript
            self._save(state)

            if judge_passed is None:
                # Judge unavailable or produced no gating signal: never gate.
                guard_passed = None
                note = "judge unavailable, answer not verified"
                break

            if judge_passed is True:
                guard_passed = True
                note = f"gated PASS after {attempt} attempt(s)"
                break

            # Failed the gate: refine if retries remain.
            if retries_used < MAX_RETRIES:
                retries_used += 1
                feedback = judge_feedback or (
                    "Previous answer failed the quality gate. Improve grounding "
                    "in the provided context and directly answer the question."
                )
                transcript.append({"role": "supervisor", "action": "refine", "retries_used": retries_used})
                continue

            guard_passed = False
            note = (
                f"gate NOT met after {attempt} attempts, returning best-effort "
                "answer flagged unverified"
            )
            break

        e2e_sec = time.perf_counter() - e2e_start
        self.metrics["e2e_latency_sec"].append(e2e_sec)
        self.metrics["requests"] += 1
        if guard_passed is True:
            self.metrics["successes"] += 1
        if guard_passed is False:
            self.metrics["unverified"] += 1
        self.metrics["retries_used"].append(retries_used)

        # Record a completion marker, then drop the state now that it is done.
        state = {
            "thread_id": thread_id,
            "transcript": transcript,
            "retries": retries_used,
            "last_draft": final_answer,
            "current_question": question,
            "tenant_slug": tenant_slug,
            "created_at": (prior or {}).get("created_at", time.time()),
        }
        self._save(state)
        self.checkpointer.delete_state(thread_id)

        return {
            "thread_id": thread_id,
            "answer": final_answer,
            "route": final_route,
            "strategy": "a2a",
            "model": self.model,
            "sources": final_sources,
            "contexts": final_contexts,
            "tenant": tenant_slug,
            "passed": guard_passed,
            "attempts": attempts_used,
            "max_retries": MAX_RETRIES,
            "transcript": transcript,
            "feedback": feedback,
            "note": note,
            "transport": served_by,
            "latency_ms": e2e_sec * 1000,
            "metrics": self.snapshot_metrics(),
        }

    def snapshot_metrics(self) -> dict:
        """Aggregate the latency/rate counters collected across requests."""

        def _agg(values: list[float]) -> dict:
            if not values:
                return {"count": 0, "avg_sec": None, "p95_sec": None}
            ordered = sorted(values)
            p95 = ordered[min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))]
            return {
                "count": len(values),
                "avg_sec": round(sum(values) / len(values), 6),
                "p95_sec": round(p95, 6),
            }

        requests = max(1, int(self.metrics["requests"]))
        successes = int(self.metrics["successes"])
        unverified = int(self.metrics["unverified"])
        retries = self.metrics["retries_used"]
        retried = sum(1 for r in retries if r > 0)

        return {
            "drafter_latency": _agg(self.metrics["drafter_latency_sec"]),
            "judge_latency": _agg(self.metrics["judge_latency_sec"]),
            "checkpointer_write_latency": _agg(self.metrics["checkpointer_write_latency_sec"]),
            "checkpointer_read_latency": _agg(self.metrics["checkpointer_read_latency_sec"]),
            "fallback_recovery": _agg(self.metrics["fallback_recovery_sec"]),
            "e2e_latency": _agg(self.metrics["e2e_latency_sec"]),
            "fallback_events": int(self.metrics["fallback_events"]),
            "requests": int(self.metrics["requests"]),
            "success_rate": round(successes / requests, 4),
            "retry_rate": round(retried / requests, 4),
            "unverified_rate": round(unverified / requests, 4),
        }
