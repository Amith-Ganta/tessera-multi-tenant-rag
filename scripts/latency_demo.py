"""Fire real queries at a running Tessera instance and print per-stage latency.

Phase 1 deliverable: prove the instrumentation end to end against the live app.
This script does NOT import the pipeline -- it talks to the HTTP API exactly like
a client would, so the numbers it prints are the same in-process ring-buffer
percentiles any operator would read from ``GET /metrics/latency`` in production.

What it does:
    1. sign up (idempotent) and log in a demo user to get a bearer token
    2. POST /ask N times (default 20) with a rotating set of questions
    3. GET /metrics/latency and print a P50/P95/P99 table in pipeline order

Run (with the app already serving on :8000):

    # start the app in one shell
    uv run uvicorn src.api.app:app --port 8000
    # then, from the project root, in another shell
    uv run python scripts/latency_demo.py

Environment overrides:
    TESSERA_BASE_URL   default http://127.0.0.1:8000
    TESSERA_DEMO_EMAIL default latency-demo@tessera.local
    TESSERA_DEMO_PASS  default latency-demo-pw
    TESSERA_DEMO_N     default 20  (number of /ask calls)
    TESSERA_DEMO_STRATEGY optional strategy name to force (e.g. adaptive, cache)

Note: /ask makes real LLM calls, so the per-request wall time is dominated by
llm_generation. That is expected and is exactly what the percentile table
surfaces -- which stage actually costs the request its time.
"""

from __future__ import annotations

import os
import sys
import time

try:
    import requests
except Exception:  # pragma: no cover - requests is a declared dependency
    print("This script needs 'requests' (a project dependency). Run via `uv run`.", file=sys.stderr)
    raise


BASE_URL = os.getenv("TESSERA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL = os.getenv("TESSERA_DEMO_EMAIL", "latency-demo@tessera.local")
PASSWORD = os.getenv("TESSERA_DEMO_PASS", "latency-demo-pw")
N = int(os.getenv("TESSERA_DEMO_N", "20"))
STRATEGY = os.getenv("TESSERA_DEMO_STRATEGY") or None

# A rotating set so retrieval/rerank see varied inputs rather than one cached hit.
QUESTIONS = [
    "What is this knowledge base about?",
    "Summarize the main topic in one sentence.",
    "What are the key components described?",
    "List any limitations or caveats mentioned.",
    "What problem does the described system solve?",
    "Are there any configuration options discussed?",
    "What technologies are referenced?",
    "Describe the overall architecture briefly.",
    "What is the recommended way to get started?",
    "Are there any security considerations noted?",
]

STAGE_ORDER = [
    "query_processing",
    "embedding",
    "vector_retrieval",
    "metadata_filtering",
    "reranking",
    "prompt_stitching",
    "llm_generation",
    "post_processing",
]


def _fail(msg: str) -> "None":
    print(f"\n[latency_demo] {msg}", file=sys.stderr)
    print(
        "[latency_demo] Is the app running? Start it with:\n"
        "    uv run uvicorn src.api.app:app --port 8000",
        file=sys.stderr,
    )
    sys.exit(1)


def _login() -> str:
    """Sign up (ignore 'already exists') then log in; return a bearer token."""
    try:
        requests.post(
            f"{BASE_URL}/auth/signup",
            json={"email": EMAIL, "password": PASSWORD},
            timeout=15,
        )
    except requests.RequestException as exc:
        _fail(f"could not reach {BASE_URL}: {exc}")

    try:
        resp = requests.post(
            f"{BASE_URL}/auth/login",
            json={"email": EMAIL, "password": PASSWORD},
            timeout=15,
        )
    except requests.RequestException as exc:
        _fail(f"login request failed: {exc}")

    if resp.status_code != 200:
        _fail(f"login failed ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("token")
    if not token:
        _fail("login succeeded but no token in response")
    return token


def _ask(token: str, question: str) -> tuple[bool, float]:
    """POST /ask once. Returns (ok, client_latency_ms)."""
    payload = {"question": question}
    if STRATEGY:
        payload["strategy"] = STRATEGY
    start = time.perf_counter()
    try:
        resp = requests.post(
            f"{BASE_URL}/ask",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
    except requests.RequestException as exc:
        print(f"    ! request error: {exc}", file=sys.stderr)
        return False, (time.perf_counter() - start) * 1000.0
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if resp.status_code != 200:
        print(f"    ! /ask {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return False, elapsed_ms
    return True, elapsed_ms


def _fetch_metrics(token: str) -> dict:
    try:
        resp = requests.get(
            f"{BASE_URL}/metrics/latency",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        _fail(f"could not fetch /metrics/latency: {exc}")
    if resp.status_code != 200:
        _fail(f"/metrics/latency returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _fmt(value) -> str:
    return "-" if value is None else f"{value:>9.3f}"


def _print_table(metrics: dict) -> None:
    stages = metrics.get("stages", {})
    header = f"{'stage':<20}{'count':>7}{'p50_ms':>10}{'p95_ms':>10}{'p99_ms':>10}{'mean_ms':>10}{'max_ms':>10}"
    print("\n" + "=" * len(header))
    print("Per-stage latency (in-process ring buffer, last "
          f"{metrics.get('sample_window', '?')} samples/stage)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    # Print known stages in pipeline order first, then anything unexpected.
    seen = set()
    for name in STAGE_ORDER:
        if name in stages:
            s = stages[name]
            seen.add(name)
            print(
                f"{name:<20}{s.get('count', 0):>7}"
                f"{_fmt(s.get('p50_ms'))}{_fmt(s.get('p95_ms'))}{_fmt(s.get('p99_ms'))}"
                f"{_fmt(s.get('mean_ms'))}{_fmt(s.get('max_ms'))}"
            )
    for name, s in stages.items():
        if name in seen:
            continue
        print(
            f"{name:<20}{s.get('count', 0):>7}"
            f"{_fmt(s.get('p50_ms'))}{_fmt(s.get('p95_ms'))}{_fmt(s.get('p99_ms'))}"
            f"{_fmt(s.get('mean_ms'))}{_fmt(s.get('max_ms'))}"
        )
    print("-" * len(header))
    print(f"total samples recorded across stages: {metrics.get('total_samples', 0)}")


def main() -> None:
    print(f"[latency_demo] target: {BASE_URL}")
    print(f"[latency_demo] firing {N} query(ies)"
          + (f" (strategy={STRATEGY})" if STRATEGY else ""))
    token = _login()

    ok_count = 0
    client_latencies: list[float] = []
    for i in range(N):
        question = QUESTIONS[i % len(QUESTIONS)]
        ok, elapsed_ms = _ask(token, question)
        client_latencies.append(elapsed_ms)
        if ok:
            ok_count += 1
        marker = "ok " if ok else "ERR"
        print(f"  [{i + 1:>2}/{N}] {marker} {elapsed_ms:>8.1f} ms  {question[:48]}")

    print(f"\n[latency_demo] {ok_count}/{N} requests succeeded")
    if ok_count == 0:
        _fail("no successful /ask calls -- nothing was recorded")

    if client_latencies:
        avg = sum(client_latencies) / len(client_latencies)
        print(f"[latency_demo] client-observed round-trip avg: {avg:.1f} ms "
              f"(includes network + full server handling)")

    metrics = _fetch_metrics(token)
    _print_table(metrics)


if __name__ == "__main__":
    main()
