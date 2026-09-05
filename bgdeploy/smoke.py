"""Automated smoke tests.

A smoke test is a single HTTP assertion (status code + optional substring
matches) against a freshly started color. bgdeploy runs the suite between
"container started" and "traffic shifted", so a broken release never sees a
single user request. The same runner is also usable standalone against any
URL (e.g. in CI, or manually with `bgdeploy smoke --url ...`).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_CASES: list[dict] = [
    {"name": "root-returns-service-info", "method": "GET", "path": "/",
     "expected_status": 200, "contains": ["service", "version"]},
    {"name": "healthz-liveness", "method": "GET", "path": "/healthz",
     "expected_status": 200, "contains": ["ok"]},
    {"name": "version-endpoint-matches-deploy", "method": "GET", "path": "/version",
     "expected_status": 200, "contains": ["{version}", "ok"]},
]


@dataclass
class SmokeCase:
    name: str
    path: str
    method: str = "GET"
    expected_status: int = 200
    contains: list[str] = field(default_factory=list)
    timeout: float = 5.0


@dataclass
class SmokeCheck:
    name: str
    ok: bool
    detail: str
    latency_ms: float


@dataclass
class SmokeReport:
    passed: bool
    checks: list[SmokeCheck]
    duration_ms: float

    @property
    def failed(self) -> list[SmokeCheck]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        return (
            f"{len(self.checks) - len(self.failed)}/{len(self.checks)} smoke checks passed "
            f"in {self.duration_ms:.0f}ms"
        )


def load_cases(path: str) -> list[SmokeCase]:
    """Load smoke cases from a JSON file; falls back to built-in defaults."""
    try:
        with open(path, encoding="utf-8") as fh:
            specs = json.load(fh)
        return [SmokeCase(**{**s, "contains": s.get("contains") or [], "timeout": s.get("timeout", 5.0)})
                for s in specs]
    except FileNotFoundError:
        return [SmokeCase(**{**s, "contains": s.get("contains") or []}) for s in DEFAULT_CASES]


def substitute(cases: list[SmokeCase], version: str) -> list[SmokeCase]:
    """Replace the {version} placeholder in paths and expected substrings."""
    if not version:
        return cases
    out = []
    for case in cases:
        out.append(
            SmokeCase(
                name=case.name,
                path=case.path.replace("{version}", version),
                method=case.method,
                expected_status=case.expected_status,
                contains=[s.replace("{version}", version) for s in case.contains],
                timeout=case.timeout,
            )
        )
    return out


def http_request(url: str, method: str = "GET", timeout: float = 5.0) -> tuple[int, str]:
    """Perform an HTTP request and return (status, body). Raises on network errors."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "bgdeploy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def http_ok(url: str, timeout: float = 5.0) -> bool:
    """Liveness probe: True when the endpoint answers 2xx/3xx."""
    try:
        status, _ = http_request(url, "GET", timeout)
        return 200 <= status < 400
    except Exception:  # noqa: BLE001 - any network problem means unhealthy
        return False


def run_smoke(base_url: str, cases: list[SmokeCase]) -> SmokeReport:
    """Run every case against base_url and aggregate the results."""
    base_url = base_url.rstrip("/")
    checks: list[SmokeCheck] = []
    start = time.monotonic()
    for case in cases:
        case_start = time.monotonic()
        url = f"{base_url}{case.path}"
        try:
            status, body = http_request(url, case.method, case.timeout)
            problems = []
            if case.expected_status is not None and status != case.expected_status:
                problems.append(f"expected status {case.expected_status}, got {status}")
            for needle in case.contains:
                if needle not in body:
                    problems.append(f"body missing {needle!r}")
            checks.append(
                SmokeCheck(
                    name=case.name,
                    ok=not problems,
                    detail="; ".join(problems) if problems else f"{status} OK",
                    latency_ms=(time.monotonic() - case_start) * 1000,
                )
            )
        except Exception as exc:  # noqa: BLE001 - request-level failure is a check failure
            checks.append(
                SmokeCheck(
                    name=case.name,
                    ok=False,
                    detail=f"request failed: {exc}",
                    latency_ms=(time.monotonic() - case_start) * 1000,
                )
            )
    duration = (time.monotonic() - start) * 1000
    return SmokeReport(passed=all(c.ok for c in checks), checks=checks, duration_ms=duration)
