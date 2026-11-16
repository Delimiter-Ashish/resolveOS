"""Resilient REST client used by the agent's tools: retries with exponential backoff +
jitter on transient failures, a per-endpoint circuit breaker, and stable idempotency keys
so every retry (and every post-crash replay) is safe."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings

TRANSIENT_STATUS = {429, 502, 503, 504}


@dataclass
class ApiResult:
    ok: bool
    status: int
    body: Any
    attempts: int = 1
    replayed: bool = False
    unavailable: bool = False  # retries exhausted / breaker open -> recovery path
    error: str = ""
    events: list[str] = field(default_factory=list)


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown: float):
        self.threshold, self.cooldown = threshold, cooldown
        self.fails: dict[str, int] = {}
        self.open_until: dict[str, float] = {}
        self.lock = threading.Lock()

    def allow(self, name: str) -> bool:
        with self.lock:
            return time.monotonic() >= self.open_until.get(name, 0.0)

    def record(self, name: str, ok: bool) -> None:
        with self.lock:
            if ok:
                self.fails[name] = 0
                return
            self.fails[name] = self.fails.get(name, 0) + 1
            if self.fails[name] >= self.threshold:
                self.open_until[name] = time.monotonic() + self.cooldown
                self.fails[name] = 0


_breaker = CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_s)
_http = httpx.Client(
    base_url=settings.api_base_url,
    timeout=httpx.Timeout(settings.tool_http_timeout_s, connect=3.0),
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
)


def request(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    json: Any = None,
    params: dict | None = None,
    idem_key: str | None = None,
    workflow_id: str | None = None,
    chaos: str | None = None,
    breaker_name: str | None = None,
    max_attempts: int | None = None,
) -> ApiResult:
    headers = {"Authorization": f"Bearer {api_key or settings.agent_api_key}"}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    if workflow_id:
        headers["X-Workflow-Id"] = workflow_id
    if chaos:
        headers["X-Chaos"] = chaos
    name = breaker_name or path
    attempts_max = max_attempts or settings.tool_max_attempts
    events: list[str] = []
    last_err = ""
    for attempt in range(1, attempts_max + 1):
        if not _breaker.allow(name):
            events.append("circuit_open")
            return ApiResult(False, 503, None, attempt - 1, unavailable=True, error="circuit_open", events=events)
        try:
            r = _http.request(method, path, json=json, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"{type(e).__name__}"
            events.append(f"retry:{last_err}")
            _breaker.record(name, False)
        else:
            if r.status_code in TRANSIENT_STATUS:
                last_err = f"http_{r.status_code}"
                events.append(f"retry:{last_err}")
                _breaker.record(name, False)
            else:
                _breaker.record(name, True)
                try:
                    body = r.json()
                except Exception:
                    body = {"raw": r.text[:300]}
                return ApiResult(
                    r.status_code < 400, r.status_code, body, attempt,
                    replayed=r.headers.get("Idempotent-Replay") == "true", events=events,
                )
        if attempt < attempts_max:
            time.sleep(settings.tool_backoff_base_s * (2 ** (attempt - 1)) * (0.7 + 0.6 * random.random()))
    return ApiResult(False, 503, None, attempts_max, unavailable=True, error=last_err or "unavailable", events=events)
