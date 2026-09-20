from locust import HttpUser, task, between
import itertools

QUESTIONS = [
    "What is RAG?",
    "How does retrieval work?",
    "What is faithfulness?",
    "Explain contextual recall.",
    "What is reranking?",
]

_LOAD_EMAIL = "loadtest@tessera.local"
_LOAD_PASS = "LoadTest123!"

_counter = itertools.count()


class TesseraUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self):
        # signup is idempotent (400 if already exists — ignore)
        self.client.post("/auth/signup", json={"email": _LOAD_EMAIL, "password": _LOAD_PASS})
        r = self.client.post("/auth/login", json={"email": _LOAD_EMAIL, "password": _LOAD_PASS})
        token = r.json().get("token", "")
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task
    def ask(self):
        i = next(_counter)
        payload = {
            "question": QUESTIONS[i % len(QUESTIONS)],
            "run_eval": (i % 10) < 3,
        }
        self.client.post("/ask", json=payload, timeout=120)
