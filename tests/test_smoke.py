from bgdeploy.smoke import (
    SmokeCase,
    http_ok,
    load_cases,
    run_smoke,
    substitute,
)


def test_smoke_passes_against_healthy_backend(backend_factory):
    server = backend_factory("blue", version="v1")
    cases = [
        SmokeCase(name="health", path="/healthz", expected_status=200, contains=["ok"]),
        SmokeCase(name="version", path="/version", expected_status=200, contains=["v1"]),
    ]
    report = run_smoke(server.url, cases)
    assert report.passed
    assert report.failed == []
    assert len(report.checks) == 2


def test_smoke_fails_when_backend_unhealthy(backend_factory):
    server = backend_factory("blue", version="v1", fail=True)
    cases = [SmokeCase(name="health", path="/healthz", expected_status=200, contains=["ok"])]
    report = run_smoke(server.url, cases)
    assert not report.passed
    assert report.failed[0].name == "health"
    assert "expected status 200, got 503" in report.failed[0].detail


def test_smoke_reports_missing_body_substring(backend_factory):
    server = backend_factory("blue", version="v1")
    cases = [SmokeCase(name="needs-flag", path="/", expected_status=200, contains=["not-there"])]
    report = run_smoke(server.url, cases)
    assert not report.passed
    assert "missing 'not-there'" in report.failed[0].detail


def test_smoke_reports_connection_error():
    cases = [SmokeCase(name="down", path="/healthz", expected_status=200)]
    report = run_smoke("http://127.0.0.1:1", cases)  # nothing listens on port 1
    assert not report.passed
    assert report.failed[0].detail.startswith("request failed")


def test_version_placeholder_is_substituted(backend_factory):
    server = backend_factory("green", version="v9.9")
    cases = [
        SmokeCase(name="version", path="/version", expected_status=200, contains=["{version}", "ok"]),
    ]
    substituted = substitute(cases, "v9.9")
    report = run_smoke(server.url, substituted)
    assert report.passed


def test_load_cases_missing_file_returns_builtins(tmp_path):
    cases = load_cases(str(tmp_path / "nope.json"))
    assert len(cases) == 3  # built-in default suite
    assert cases[0].name == "root-returns-service-info"


def test_http_ok_true_and_false(backend_factory):
    server = backend_factory("blue")
    assert http_ok(server.url + "/healthz")
    server.set_fail(True)
    assert not http_ok(server.url + "/healthz")
    assert not http_ok("http://127.0.0.1:1/healthz")
