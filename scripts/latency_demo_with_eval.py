"""Fire real queries with DeepEval judging ON, then print per-stage latency.

Phase 3 (3b/3d) deliverable: prove that the JUDGE stage shows up as its own row
on ``GET /metrics/latency`` under real load, and that the judge's cost is visible
separately from llm_generation.

This is the sibling of ``latency_demo.py``. The only behavioural differences:

    1. every /ask carries ``run_eval: true`` so the DeepEval judge runs and each
       ``metric.measure()`` is timed under Stage.JUDGE
    2. the query set includes three adversarial, low-confidence questions whose
       answers are not in the corpus -- these drive the confidence gate low, so
       the re-ranker and the refinement loop both engage and the judge sees
       genuinely marginal answers rather than only easy ones
    3. the printed table adds the ``judge`` row after ``post_processing``

Like latency_demo.py, this talks to the HTTP API exactly as a client would, so
the numbers are the same in-process ring-buffer percentiles an operator reads in
production. It imports nothing from the pipeline.

Run (with the app already serving on :8000):

    # start the app in one shell
    uv run python -m uvicorn src.api.app:app --port 8000
    # then, from the project root, in another shell
    uv run python scripts/latency_demo_with_eval.py

Environment overrides:
    TESSERA_BASE_URL   default http://127.0.0.1:8000
    TESSERA_DEMO_EMAIL default latency-eval-demo@tessera.local
    TESSERA_DEMO_PASS  default latency-eval-demo-pw
    TESSERA_DEMO_N     default 20  (number of /ask calls)
    TESSERA_DEMO_STRATEGY optional strategy name to force (e.g. adaptive, cache)

Note: with run_eval on, every request pays for both the answer LLM and several
judge LLM calls, so wall time per request is much higher than the plain demo.
That is expected: the point is to populate and surface the judge percentiles.
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
EMAIL = os.getenv("TESSERA_DEMO_EMAIL", "latency-eval-demo@tessera.local")
PASSWORD = os.getenv("TESSERA_DEMO_PASS", "latency-eval-demo-pw")
N = int(os.getenv("TESSERA_DEMO_N", "20"))
STRATEGY = os.getenv("TESSERA_DEMO_STRATEGY") or None

# Ordinary in-corpus questions -- these should answer with reasonable confidence.
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

# Adversarial, low-confidence questions. Their answers are deliberately NOT in the
# corpus, so top-1 similarity comes back low: the re-ranker runs, the refinement
# loop may trigger, and the judge scores genuinely marginal answers. These are the
# cases that exercise the full guard + judge path, not just the happy path.
ADVERSARIAL = [
    "What was the closing share price of Acme Corp on 12 March 1998?",
    "How many employees does the Antarctic branch office currently have?",
    "What is the exact recipe for the author's grandmother's plum cake?",
]


def _build_query_plan(total: int) -> list[str]:
    """Interleave the three adversarial queries into the rotating normal set.

    We guarantee all three adversarial queries appear (spec: at least three
    low-confidence queries) and fill the remainder from the normal rotation.
    """
    plan: list[str] = []
    normal_i = 0
    # Fixed slots for the adversarial queries, spread across the run.
    adversarial_slots = {3, 9, 15}
    adv_iter = iter(ADVERSARIAL)
    for i in range(total):
        if i in adversarial_slots:
            try:
                plan.append(next(adv_iter))
                continue
            except StopIteration:
                pass
        plan.append(QUESTIONS[normal_i % len(QUESTIONS)])
        normal_i += 1
    # If total was tiny and some adversarial queries never got placed, append them.
    for leftover in adv_iter:
        plan.append(leftover)
    return plan


STAGE_ORDER = [
    "query_processing",
    "embedding",
    "vector_retrieval",
    "metadata_filtering",
    "reranking",
    "prompt_stitching",
    "llm_generation",
    "post_processing",
    "judge",
]


def _fail(msg: str) -> "None":
    print(f"\n[latency_demo_with_eval] {msg}", file=sys.stderr)
    print(
        "[latency_demo_with_eval] Is the app running? Start it with:\n"
        "    uv run python -m uvicorn src.api.app:app --port 8000",
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
    """POST /ask once with run_eval on. Returns (ok, client_latency_ms)."""
    payload = {"question": question, "run_eval": True}
    if STRATEGY:
        payload["strategy"] = STRATEGY
    start = time.perf_counter()
    try:
        resp = requests.post(
            f"{BASE_URL}/ask",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,  # judging adds several extra LLM calls per request
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
    print(f"[latency_demo_with_eval] target: {BASE_URL}")
    print(f"[latency_demo_with_eval] firing {N} query(ies) with run_eval=True"
          + (f" (strategy={STRATEGY})" if STRATEGY else ""))
    print("[latency_demo_with_eval] 3 adversarial low-confidence queries are mixed in")
    token = _login()

    plan = _build_query_plan(N)
    ok_count = 0
    client_latencies: list[float] = []
    for i, question in enumerate(plan):
        is_adv = question in ADVERSARIAL
        ok, elapsed_ms = _ask(token, question)
        client_latencies.append(elapsed_ms)
        if ok:
            ok_count += 1
        marker = "ok " if ok else "ERR"
        tag = "[ADV] " if is_adv else "      "
        print(f"  [{i + 1:>2}/{len(plan)}] {marker} {elapsed_ms:>8.1f} ms  {tag}{question[:48]}")

    print(f"\n[latency_demo_with_eval] {ok_count}/{len(plan)} requests succeeded")
    if ok_count == 0:
        _fail("no successful /ask calls -- nothing was recorded")

    if client_latencies:
        avg = sum(client_latencies) / len(client_latencies)
        print(f"[latency_demo_with_eval] client-observed round-trip avg: {avg:.1f} ms "
              f"(includes network + full server handling + judging)")

    metrics = _fetch_metrics(token)
    _print_table(metrics)

    judge = metrics.get("stages", {}).get("judge")
    if judge and judge.get("count", 0) > 0:
        print(f"\n[latency_demo_with_eval] JUDGE stage recorded {judge['count']} samples "
              f"(p50={_fmt(judge.get('p50_ms')).strip()} ms) -- judge cost is now visible "
              "separately from llm_generation.")
    else:
        print("\n[latency_demo_with_eval] WARNING: no judge samples recorded. "
              "Was run_eval honoured and the OpenAI judge key available?", file=sys.stderr)


if __name__ == "__main__":
    main()
