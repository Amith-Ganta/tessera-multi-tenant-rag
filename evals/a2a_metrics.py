"""Measure the A2A implementation and write ``evals/reports/a2a_implementation_metrics.json``.

Two classes of measurement are produced:

1. **Offline (no LLM, always runs)** — SQLite checkpointer write/read latency,
   A2A JSON-RPC protocol round-trip latency (Drafter + Judge) against stub
   handlers, and fallback recovery time (agent unreachable -> in-process
   recovery). These exercise the real protocol and checkpointer code paths
   without spending any tokens.

2. **Live (optional, needs keys + index)** — an end-to-end in-process run of the
   A2A supervisor over the first golden question, which populates the real
   Drafter/Judge generation latencies, end-to-end latency, and the
   success/retry/unverified rates. When ``DEEPSEEK_API_KEY`` and
   ``OPENAI_API_KEY`` are absent, or the index is not built, these fields are
   reported as ``null`` with an explanatory note instead of fabricating numbers.

Run:

    uv run python -m evals.a2a_metrics
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = REPORTS_DIR / "a2a_implementation_metrics.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agg(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "avg_sec": None, "p95_sec": None, "min_sec": None, "max_sec": None}
    ordered = sorted(values)
    p95_idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "n": len(values),
        "avg_sec": round(mean(values), 6),
        "p95_sec": round(ordered[p95_idx], 6),
        "min_sec": round(ordered[0], 6),
        "max_sec": round(ordered[-1], 6),
    }


def _measure_checkpointer(n: int = 50) -> dict:
    """Real SQLite write/read/delete latency over a temp database."""
    from src.rag.checkpointer import SQLiteCheckpointer

    tmp = Path(tempfile.mkdtemp()) / "checkpoints.sqlite3"
    ck = SQLiteCheckpointer(tmp)

    write_sec: list[float] = []
    read_sec: list[float] = []
    delete_sec: list[float] = []

    state = {
        "transcript": [{"attempt": 1, "role": "drafter", "draft": "draft"}],
        "retries": 0,
        "last_draft": "draft",
        "current_question": "what is SOC 2?",
        "tenant_slug": "default",
        "created_at": time.time(),
    }

    for i in range(n):
        tid = f"thread-{i}"

        start = time.perf_counter()
        ck.save_state(tid, state)
        write_sec.append(time.perf_counter() - start)

        start = time.perf_counter()
        loaded = ck.load_state(tid)
        read_sec.append(time.perf_counter() - start)
        assert loaded is not None

        start = time.perf_counter()
        ck.delete_state(tid)
        delete_sec.append(time.perf_counter() - start)

    # Also prove resume round-trips: save once, load back the exact fields.
    ck.save_state("roundtrip", state)
    rt = ck.load_state("roundtrip")
    assert rt is not None and rt["last_draft"] == "draft" and rt["retries"] == 0
    ck.delete_state("roundtrip")

    return {
        "write": _agg(write_sec),
        "read": _agg(read_sec),
        "delete": _agg(delete_sec),
        "roundtrip_verified": True,
        "db": str(tmp),
    }


def _measure_a2a_protocol(n: int = 25) -> dict:
    """A2A JSON-RPC round-trip latency against stub Drafter/Judge handlers."""
    from fastapi.testclient import TestClient

    from src.agents.a2a_protocol import make_a2a_app

    drafter_app = make_a2a_app(
        "stub-drafter", "stub", "http://localhost:8001",
        "draft_answer", "draft_answer", "stub",
        lambda params: {"draft": "stub draft", "context": ["c"], "tenant": params.get("tenant_slug", "")},
    )
    judge_app = make_a2a_app(
        "stub-judge", "stub", "http://localhost:8002",
        "judge_answer", "judge_answer", "stub",
        lambda params: {"score": {"faithfulness": 1.0}, "passed": True, "feedback": None},
    )

    drafter = TestClient(drafter_app)
    judge = TestClient(judge_app)

    drafter_sec: list[float] = []
    judge_sec: list[float] = []

    def rpc(metadata: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": [], "metadata": metadata}},
        }

    # Warm up (imports / first request).
    drafter.post("/", json=rpc({"question": "q", "tenant_slug": "default"}))
    judge.post("/", json=rpc({"draft": "d", "context": [], "tenant_slug": "default"}))

    for _ in range(n):
        start = time.perf_counter()
        resp = drafter.post("/", json=rpc({"question": "q", "tenant_slug": "default"}))
        assert resp.status_code == 200
        drafter_sec.append(time.perf_counter() - start)

        start = time.perf_counter()
        resp = judge.post("/", json=rpc({"draft": "d", "context": [], "tenant_slug": "default"}))
        assert resp.status_code == 200
        judge_sec.append(time.perf_counter() - start)

    # Also verify AgentCard discovery works.
    card = drafter.get("/.well-known/agent.json")
    assert card.status_code == 200 and card.json().get("skills")

    return {
        "drafter_roundtrip": _agg(drafter_sec),
        "judge_roundtrip": _agg(judge_sec),
        "agent_card_discovery_ok": True,
    }


def _measure_fallback_recovery() -> dict:
    """Time the supervisor's recovery when the Drafter agent is unreachable."""
    from src.orchestrator.a2a_supervisor import A2ASupervisor

    # Pick a port that is almost certainly closed so the HTTP drafter call fails.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    supervisor = A2ASupervisor(
        drafter_url=f"http://127.0.0.1:{closed_port}",
        judge_url="http://127.0.0.1:1",
        mode="http",
    )

    # Replace the in-process fallback with a stub that returns immediately, so the
    # measurement reflects the recovery machinery (catch -> fallback) and not an
    # LLM call. This mirrors what happens when the real Drafter recovers.
    # NOTE: the supervisor's _call_drafter_in_process takes (question, tenant,
    # feedback, previous_draft); the 5th _call_drafter arg is the transcript list.
    supervisor._call_drafter_in_process = lambda q, t, f, p: {  # type: ignore[method-assign]
        "draft": "recovered draft", "context": [], "tenant": t,
        "route": "vector", "sources": [], "usage": {}, "trace": [],
    }

    recovery_samples: list[float] = []
    transcript: list = []
    for _ in range(10):
        # Reset to http each round so every iteration exercises the fallback path.
        supervisor.mode = "http"
        start = time.perf_counter()
        payload, transport = supervisor._call_drafter("q", "default", None, None, transcript)
        total = time.perf_counter() - start
        assert payload.get("draft") == "recovered draft"
        recovery_samples.append(total)

    # The supervisor records per-fallback recovery in its metrics.
    fallback_sec = supervisor.metrics.get("fallback_recovery_sec", [])
    return {
        "recovery": _agg(fallback_sec or recovery_samples),
        "total_including_detection": _agg(recovery_samples),
        "fallback_events": int(supervisor.metrics.get("fallback_events", 0)),
    }


def _detect_tenant() -> str | None:
    """Pick a tenant that has both a built index and a corpus, preferring the
    tenant seeded with the full 12-file golden corpus (user-guardproof)."""
    from src.rag.config import PROJECT_ROOT

    tenants_dir = PROJECT_ROOT / "data" / "index" / "tenants"
    corpus_dir = PROJECT_ROOT / "data" / "tenants"
    if not tenants_dir.exists():
        return None

    candidates: list[str] = []
    for p in sorted(tenants_dir.iterdir()):
        if not p.is_dir():
            continue
        has_index = (p / "chroma" / "chroma.sqlite3").exists()
        corpus = corpus_dir / p.name / "corpus"
        has_corpus = corpus.exists() and any(corpus.rglob("*"))
        if has_index and has_corpus:
            candidates.append(p.name)

    for preferred in ("user-guardproof", "tenant-a", "tenant-b"):
        if preferred in candidates:
            return preferred
    return candidates[0] if candidates else None


def _try_live_e2e() -> dict | None:
    """Run real in-process end-to-end runs through the supervisor, if possible."""
    import os

    from src.rag.config import GOLDENS_PATH

    if not os.getenv("DEEPSEEK_API_KEY") or not os.getenv("OPENAI_API_KEY"):
        return None

    tenant = os.getenv("A2A_METRICS_TENANT", "").strip() or _detect_tenant()
    if not tenant:
        return None

    # Verify the tenant index actually exists (not just the default index).
    from src.rag.config import PROJECT_ROOT

    tenant_index = PROJECT_ROOT / "data" / "index" / "tenants" / tenant / "chroma"
    if not tenant_index.exists():
        return None

    try:
        goldens = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
        questions = [
            (g.get("input") or g.get("question") or "").strip()
            for g in goldens
            if (g.get("input") or g.get("question") or "").strip()
        ]
    except Exception:
        questions = ["What is SOC 2?"]

    n = int(os.getenv("A2A_METRICS_N", "5") or "5")
    questions = questions[: max(1, min(n, len(questions)))]

    from src.orchestrator.a2a_supervisor import A2ASupervisor

    supervisor = A2ASupervisor(mode="in_process")
    per_question: list[dict] = []
    errors: list[str] = []

    for q in questions:
        try:
            result = supervisor.process_question(
                q, tenant, run_eval=True, expected_output=None
            )
            per_question.append(
                {
                    "question": q[:80],
                    "passed": result.get("passed"),
                    "attempts": result.get("attempts"),
                    "retries_used": result.get("metrics", {}).get("retries_used"),
                    "answer_length": len(result.get("answer", "")),
                    "note": result.get("note", ""),
                }
            )
        except Exception as exc:  # noqa: BLE001 - best-effort live run
            errors.append(f"{q[:40]}: {type(exc).__name__}: {exc}")
            per_question.append({"question": q[:80], "error": f"{type(exc).__name__}: {exc}"})

    metrics = supervisor.snapshot_metrics()

    def _pct(ratio: float | None) -> float | None:
        return round(ratio * 100.0, 2) if isinstance(ratio, (int, float)) else None

    return {
        "ran": True,
        "tenant": tenant,
        "questions_run": len(per_question),
        "errors": errors,
        "passed_count": sum(1 for r in per_question if r.get("passed") is True),
        "unverified_count": sum(1 for r in per_question if r.get("passed") is False),
        "e2e_latency": metrics.get("e2e_latency"),
        "drafter_latency": metrics.get("drafter_latency"),
        "judge_latency": metrics.get("judge_latency"),
        "checkpointer_write_latency": metrics.get("checkpointer_write_latency"),
        "checkpointer_read_latency": metrics.get("checkpointer_read_latency"),
        "success_rate_pct": _pct(metrics.get("success_rate")),
        "retry_rate_pct": _pct(metrics.get("retry_rate")),
        "unverified_rate_pct": _pct(metrics.get("unverified_rate")),
        "fallback_events": metrics.get("fallback_events"),
        "per_question": per_question,
    }


def build_report() -> dict:
    print("Measuring checkpointer latency...")
    checkpointer = _measure_checkpointer()

    print("Measuring A2A protocol latency...")
    protocol = _measure_a2a_protocol()

    print("Measuring fallback recovery...")
    fallback = _measure_fallback_recovery()

    print("Attempting live end-to-end (requires API keys + index)...")
    if os.environ.get("A2A_METRICS_LIVE") == "1":
        live = _try_live_e2e()
    else:
        live = {
            "ran": False,
            "reason": (
                "live end-to-end skipped (set A2A_METRICS_LIVE=1 to enable; "
                "requires DEEPSEEK_API_KEY, OPENAI_API_KEY, and a built tenant index)"
            ),
        }

    return {
        "timestamp": _now_iso(),
        "note": (
            "Offline metrics (checkpointer, A2A protocol, fallback recovery) are "
            "measured directly. Live LLM metrics are null unless API keys and a "
            "built index were present at run time."
        ),
        "checkpointer": checkpointer,
        "a2a_protocol": protocol,
        "fallback_recovery": fallback,
        "live_end_to_end": live,
    }


def main() -> None:
    report = build_report()
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
