"""Traffic shifting.

Weights are a tiny JSON file ({"blue": 100, "green": 0}) that the router
container reads on a poll loop. bgdeploy rewrites this file to shift traffic
gradually (25 -> 50 -> 75 -> 100%), waits for the router to pick up each step,
and finally *verifies* from the client side that all sampled requests are
really served by the intended color before it commits the deployment.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter

from .smoke import http_request

DEFAULT_WEIGHTS = {"blue": 100, "green": 0}


class TrafficWeightsStore:
    def __init__(self, path: str, fallback: dict | None = None):
        self.path = path
        self._fallback = dict(fallback) if fallback is not None else dict(DEFAULT_WEIGHTS)

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return {c: int(data.get(c, 0)) for c in ("blue", "green")}
        except (OSError, ValueError, json.JSONDecodeError):
            return dict(self._fallback)

    def current(self) -> dict:
        weights = self.load()
        if sum(weights.values()) <= 0:  # empty/invalid -> fall back
            weights = dict(self._fallback)
        return weights

    def set(self, color: str, percent: int) -> dict:
        """Set color's share of traffic (0-100); the other color gets the rest."""
        if color not in ("blue", "green"):
            raise ValueError(f"unknown color {color!r}")
        percent = max(0, min(100, int(percent)))
        other = "green" if color == "blue" else "blue"
        weights = {"blue": 0, "green": 0}
        weights[color] = percent
        weights[other] = 100 - percent
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(weights, fh)
        return weights


def sample_served(router_url: str, samples: int = 10, timeout: float = 5.0) -> dict:
    """Ask the router to proxy `samples` requests and tally which color served.

    The router stamps every proxied response with X-Served-By, so we can
    observe real traffic distribution from outside the stack.
    """
    counts: Counter = Counter()
    errors = 0
    url = router_url.rstrip("/") + "/"
    for _ in range(samples):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bgdeploy/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                served = resp.headers.get("X-Served-By")
                if served:
                    counts[served] += 1
                else:
                    errors += 1
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            errors += 1
    return {"counts": dict(counts), "errors": errors}


def verify_full_shift(
    router_url: str, expected_color: str, samples: int = 10, timeout: float = 5.0
) -> tuple[bool, dict]:
    """Verify that 100% of sampled traffic is served by expected_color."""
    result = sample_served(router_url, samples=samples, timeout=timeout)
    counts = result["counts"]
    ok = (
        result["errors"] == 0
        and counts.get(expected_color, 0) == samples
        and sum(counts.values()) == samples
    )
    return ok, result


def fetch_router_health(router_url: str, timeout: float = 5.0) -> dict:
    """Read the router's /__router/health JSON."""
    status, body = http_request(router_url.rstrip("/") + "/__router/health", "GET", timeout)
    if status != 200:
        raise RuntimeError(f"router health endpoint returned {status}")
    return json.loads(body)
