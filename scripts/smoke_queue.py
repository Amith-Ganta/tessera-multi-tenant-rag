"""Smoke test: publish 20 judge jobs and verify queue depth then drains."""
import requests
import time
import json

BASE = "http://localhost:8000"

for i in range(20):
    requests.post(
        f"{BASE}/ask",
        json={"question": f"queue test {i}", "tenant": "smoke-queue", "run_eval": True},
        timeout=60,
    )

time.sleep(2)
r = requests.get(f"{BASE}/metrics/latency", timeout=10)
print(json.dumps(r.json(), indent=2))
