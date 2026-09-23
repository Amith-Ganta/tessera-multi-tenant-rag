"""Tessera Multi-Tenant RAG API — Locust load test.

Usage (against a locally running instance):
    locust -f loadtests/locustfile.py --host http://127.0.0.1:8000

Environment variables:
    TESSERA_LOAD_USER     username for test tenant  (default: loadtest_user)
    TESSERA_LOAD_PASS     password for test tenant  (default: loadtest_pass)
    TESSERA_LOAD_THREADS  JWT token thread count  — ignored; token is obtained at
                          user creation time via on_start()

The test assumes:
  - A running Tessera instance on --host.
  - POST /register is open (no auth).
  - Each simulated user self-registers on start to get a fresh tenant JWT.
  - A corpus document has already been uploaded (or TaskSet uploads one first).

Target throughput during a baseline capacity run:
  - 50 virtual users, spawn rate 5/s.
  - Expected: p50 < 2 s, p99 < 8 s, error rate < 1 %.
"""

from __future__ import annotations

import json
import os
import time

from locust import HttpUser, SequentialTaskSet, between, task, events


_LOAD_USER = os.environ.get("TESSERA_LOAD_USER", "loadtest_user")
_LOAD_PASS = os.environ.get("TESSERA_LOAD_PASS", "loadtest_pass")

_SAMPLE_QUESTIONS = [
    "What are the main topics in the uploaded document?",
    "Summarise the key points briefly.",
    "What does the document say about costs?",
    "List any dates or deadlines mentioned.",
    "What are the conclusions or recommendations?",
]


class AskTaskSet(SequentialTaskSet):
    """Ordered task set: register → upload sample doc → run /ask queries."""

    token: str = ""
    _question_idx: int = 0

    def on_start(self) -> None:
        """Register a user and obtain a JWT; upload a one-page corpus doc."""
        # Use a unique username per user instance to avoid registration collisions.
        suffix = int(time.time() * 1000) % 100_000
        username = f"{_LOAD_USER}_{suffix}"
        password = _LOAD_PASS

        resp = self.client.post(
            "/register",
            json={"username": username, "password": password},
            name="/register",
        )
        if resp.status_code not in (200, 201):
            self.token = ""
            return

        resp = self.client.post(
            "/token",
            data={"username": username, "password": password},
            name="/token",
        )
        if resp.status_code == 200:
            self.token = resp.json().get("access_token", "")

        if self.token:
            _upload_sample_corpus(self.client, self.token)

    @task(8)
    def ask_adaptive(self) -> None:
        if not self.token:
            return
        question = _SAMPLE_QUESTIONS[self._question_idx % len(_SAMPLE_QUESTIONS)]
        self._question_idx += 1
        self.client.post(
            "/ask",
            json={"question": question, "strategy": "adaptive"},
            headers={"Authorization": f"Bearer {self.token}"},
            name="/ask [adaptive]",
        )

    @task(2)
    def ask_dense(self) -> None:
        if not self.token:
            return
        self.client.post(
            "/ask",
            json={"question": "What are the main topics?", "strategy": "dense"},
            headers={"Authorization": f"Bearer {self.token}"},
            name="/ask [dense]",
        )

    @task(1)
    def health_check(self) -> None:
        self.client.get("/health", name="/health")

    @task(1)
    def metrics_check(self) -> None:
        self.client.get("/metrics", name="/metrics")


class TesseraUser(HttpUser):
    tasks = [AskTaskSet]
    wait_time = between(1, 3)
    host = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# Admin user — exercises /admin/dlq endpoints under load
# ---------------------------------------------------------------------------

class AdminTaskSet(SequentialTaskSet):
    token: str = ""

    def on_start(self) -> None:
        resp = self.client.post(
            "/token",
            data={"username": "admin", "password": os.environ.get("TESSERA_ADMIN_PASS", "admin_pass")},
            name="/token [admin]",
        )
        if resp.status_code == 200:
            self.token = resp.json().get("access_token", "")

    @task(1)
    def peek_dlq(self) -> None:
        if not self.token:
            return
        self.client.get(
            "/admin/dlq",
            headers={"Authorization": f"Bearer {self.token}"},
            name="/admin/dlq [GET]",
        )


class TesseraAdminUser(HttpUser):
    tasks = [AdminTaskSet]
    wait_time = between(10, 30)
    weight = 1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_SAMPLE_CORPUS_TEXT = (
    "Tessera load test corpus document.\n"
    "This document is uploaded by the load test user to populate the tenant corpus.\n"
    "It covers: costs, deadlines (Q4 2026), and recommendations for system scaling.\n"
    "The main recommendation is to increase replica count during peak hours.\n"
    "Conclusion: monitor latency and cost per request weekly.\n"
)


def _upload_sample_corpus(client, token: str) -> None:
    """Upload a minimal in-memory text file as the tenant corpus."""
    client.post(
        "/upload",
        files={"file": ("loadtest_corpus.txt", _SAMPLE_CORPUS_TEXT.encode(), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
        name="/upload [setup]",
    )
