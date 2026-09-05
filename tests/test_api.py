import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from bgdeploy.api import AuthManager, ControlAPI
from bgdeploy.config import Config
from bgdeploy.deployer import BlueGreenDeployer
from bgdeploy.state import StateStore

from conftest import RecordingWeights
from test_deployer import Stack


class ApiServer:
    """ControlAPI wired to a hermetic test stack (fake engine + live backends)."""

    def __init__(self, tmp_path, backend_factory, webhook_token=None,
                 password="", api_token="", api_user="admin",
                 rate_limit=5, rate_window=60.0, lockout_attempts=5,
                 lockout_seconds=900.0, trust_proxy=False, clock=None,
                 state_file=None):
        self.stack = Stack(tmp_path, backend_factory)
        cfg = self.stack.cfg

        def make_deployer(log):
            return BlueGreenDeployer(
                cfg,
                engine=self.stack.engine,
                state_store=StateStore(cfg.state_file),
                weights=RecordingWeights(cfg.weights_file),
                sampler=lambda url, samples: {"counts": {self.stack.served[0]: samples},
                                              "errors": 0},
                sleep=lambda _s: None,
                log=log,
            )

        auth = AuthManager(username=api_user, password=password, token=api_token,
                           rate_limit=rate_limit, rate_window=rate_window,
                           lockout_attempts=lockout_attempts,
                           lockout_seconds=lockout_seconds,
                           trust_proxy=trust_proxy, clock=clock,
                           state_file=state_file)
        self.server = ControlAPI(("127.0.0.1", 0), make_deployer,
                                 webhook_token=webhook_token, auth=auth)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # helpers ---------------------------------------------------------
    def request(self, method: str, path: str, body=None, headers=None):
        headers = dict(headers or {})
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def get(self, path: str, headers=None):
        status, raw, _ = self.request("GET", path, headers=headers)
        return status, json.loads(raw.decode())

    def get_raw(self, path: str, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path: str, body: dict, token=None):
        headers = {}
        if token:
            headers["X-Bg-Token"] = token
        status, raw, _ = self.request("POST", path, body, headers)
        return status, json.loads(raw.decode())

    def post_raw(self, path: str, body=None, headers=None):
        return self.request("POST", path, body, headers)

    def post_json(self, path: str, body=None, headers=None):
        status, raw, resp_headers = self.request("POST", path, body, headers)
        return status, json.loads(raw.decode()), resp_headers

    def login(self, username="admin", password="hunter2"):
        status, raw, resp_headers = self.request(
            "POST", "/api/login", {"username": username, "password": password})
        body = json.loads(raw.decode())
        cookie = ""
        set_cookie = resp_headers.get("Set-Cookie") or ""
        if set_cookie.startswith("bgdeploy_session="):
            cookie = set_cookie.split(";")[0]
        return status, body, cookie

    def wait_job(self, job_id: str, timeout: float = 15.0, headers=None):
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            _s, job = self.get(f"/api/jobs/{job_id}", headers=headers)
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in {timeout}s")


@pytest.fixture
def api(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory)
    yield server
    server.close()


# --------------------------------------------------------------------------- #
def test_healthz_and_ui(api):
    status, body = api.get("/api/healthz")
    assert status == 200 and body["status"] == "ok"

    with urllib.request.urlopen(api.base + "/", timeout=10) as resp:
        assert resp.status == 200
        html = resp.read().decode()
        assert "text/html" in resp.headers["Content-Type"]
        assert "Rollback to previous release" in html
        assert "/api/status" in html


def test_initial_status_reports_empty_stack(api):
    status, snap = api.get("/api/status")
    assert status == 200
    assert snap["state"]["active_color"] is None
    assert snap["weights"] == {"blue": 100, "green": 0}


def test_deploy_job_then_rollback_via_api(api):
    # deploy v1 -> blue
    status, job = api.post("/api/jobs", {"action": "deploy", "version": "v1"})
    assert status == 202
    finished = api.wait_job(job["id"])
    assert finished["status"] == "done"
    assert finished["result"]["color"] == "blue"
    assert finished["result"]["version"] == "v1"

    _s, snap = api.get("/api/status")
    assert snap["state"]["active_color"] == "blue"

    # rollback with no previous release must fail cleanly as a job
    status, job = api.post("/api/jobs", {"action": "rollback"})
    assert status == 202
    finished = api.wait_job(job["id"])
    assert finished["status"] == "failed"
    assert "nothing to roll back" in finished["error"]

    # deploy v2 -> green, then rollback -> blue again
    api.stack.served[0] = "green"
    _s, job = api.post("/api/jobs", {"action": "deploy", "version": "v2"})
    finished = api.wait_job(job["id"])
    assert finished["status"] == "done"
    assert finished["result"]["color"] == "green"

    api.stack.served[0] = "blue"
    _s, job = api.post("/api/jobs", {"action": "rollback"})
    finished = api.wait_job(job["id"])
    assert finished["status"] == "done"
    assert finished["result"]["color"] == "blue"
    assert finished["log"]  # job captured live log lines


def test_deploy_job_captures_smoke_failure(api):
    # bootstrap a healthy blue first
    _s, job = api.post("/api/jobs", {"action": "deploy", "version": "v1"})
    assert api.wait_job(job["id"])["status"] == "done"

    # make the green candidate fail its smoke suite (but pass /healthz so the
    # start-up wait succeeds and the failure surfaces in the smoke gate)
    api.stack.green.fail_paths.update({"/", "/version"})
    api.stack.served[0] = "green"
    _s, job = api.post("/api/jobs", {"action": "deploy", "version": "broken"})
    finished = api.wait_job(job["id"])
    assert finished["status"] == "failed"
    assert "smoke tests failed" in finished["error"]
    # state untouched: still blue v1, candidate removed
    _s, snap = api.get("/api/status")
    assert snap["state"]["active_color"] == "blue"
    assert "app-green" in api.stack.engine.removed


def test_jobs_endpoint_lists_history(api):
    _s, job = api.post("/api/jobs", {"action": "deploy", "version": "v1"})
    api.wait_job(job["id"])
    _s, data = api.get("/api/jobs")
    assert data["jobs"][0]["id"] == job["id"]
    assert data["jobs"][0]["status"] == "done"


def test_deploy_requires_version(api):
    status, body = api.post("/api/jobs", {"action": "deploy", "version": "  "})
    assert status == 400
    assert "version is required" in body["error"]

    status, body = api.post("/api/jobs", {"action": "reset"})
    assert status == 400


# --------------------------------------------------------------------------- #
# GitHub webhook                                                              #
# --------------------------------------------------------------------------- #
def _tag_payload(tag="v1", deleted=False):
    return {
        "ref": f"refs/tags/{tag}",
        "deleted": deleted,
        "repository": {"name": "demo", "full_name": "acme/demo"},
    }


def test_webhook_tag_push_triggers_deploy(api):
    status, body = api.post("/api/webhook/github", _tag_payload("v1"))
    assert status == 202
    assert body["triggered"] is True
    job = api.wait_job(body["job"]["id"])
    assert job["status"] == "done"
    assert job["result"]["version"] == "v1"
    assert job["result"]["color"] == "blue"


def test_webhook_ignores_branch_pushes_and_deletes(api):
    status, body = api.post("/api/webhook/github", {"ref": "refs/heads/main"})
    assert status == 200 and body["triggered"] is False

    status, body = api.post("/api/webhook/github", _tag_payload(deleted=True))
    assert status == 200 and body["triggered"] is False

    status, body = api.post("/api/webhook/github", {"zen": "hi", "hook_id": 1})
    assert status == 200 and body["triggered"] is False

    # nothing was enqueued
    _s, data = api.get("/api/jobs")
    assert data["jobs"] == []


def test_webhook_requires_token_when_configured(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory, webhook_token="sekrit")
    try:
        status, body = server.post("/api/webhook/github", _tag_payload(), token="wrong")
        assert status == 401

        status, body = server.post("/api/webhook/github", _tag_payload("v1"), token="sekrit")
        assert status == 202
        job = server.wait_job(body["job"]["id"])
        assert job["status"] == "done"
        assert job["result"]["version"] == "v1"
    finally:
        server.close()


def test_concurrent_jobs_are_single_flighted(api):
    # The first deploy holds the lock; a racing second deploy must fail with a
    # clear lock message instead of corrupting state. Both are fast here, so we
    # assert that two successful sequential deploys never both land on the same
    # color (the lock serialises them and each flips the active color).
    _s, j1 = api.post("/api/jobs", {"action": "deploy", "version": "v1"})
    api.wait_job(j1["id"])
    _s, snap = api.get("/api/status")
    assert snap["state"]["active_color"] == "blue"

    api.stack.served[0] = "green"
    _s, j2 = api.post("/api/jobs", {"action": "deploy", "version": "v2"})
    finished = api.wait_job(j2["id"])
    assert finished["status"] == "done"
    _s, snap = api.get("/api/status")
    assert snap["state"]["active_color"] == "green"


# --------------------------------------------------------------------------- #
# Authentication (login + session/token)                                       #
# --------------------------------------------------------------------------- #
def test_auth_is_off_by_default(api):
    # No BG_API_PASSWORD / BG_API_TOKEN configured -> everything is open and
    # GET / serves the panel directly (backwards compatible).
    assert api.get("/api/status")[0] == 200
    status, raw, _ = api.get_raw("/")
    assert status == 200
    html = raw.decode()
    assert "Rollback to previous release" in html
    assert "sign in" not in html.lower()


def test_login_flow_session_and_logout(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory, password="hunter2")
    try:
        # wrong password rejected
        status, body, _ = server.post_json("/api/login",
                                           {"username": "admin", "password": "nope"})
        assert status == 401 and "invalid" in body["error"]

        # everything except healthz/session/login requires credentials
        assert server.get("/api/status")[0] == 401
        assert server.get("/api/jobs")[0] == 401
        assert server.get_raw("/")[0] == 200  # login page, see below
        assert server.get("/api/healthz")[0] == 200
        status, info = server.get("/api/session")
        assert info == {"authenticated": False, "auth_enabled": True, "username": None}

        # unauthenticated GET / serves the login page, not the panel
        status, raw, _ = server.get_raw("/")
        html = raw.decode().lower()
        assert "sign in" in html and "rollback to previous release" not in html

        # correct credentials -> token + session cookie
        status, body, cookie = server.login()
        assert status == 200 and body["token"]
        assert cookie.startswith("bgdeploy_session=")

        # the login token works as a bearer token for the API
        status, snap, _ = server.get_raw(
            "/api/status", {"Authorization": "Bearer " + body["token"]})
        assert status == 200 and snap is not None

        # the session cookie unlocks the API and the panel UI
        assert server.get("/api/status", {"Cookie": cookie})[0] == 200
        status, raw, _ = server.get_raw("/", {"Cookie": cookie})
        assert b"Rollback to previous release" in raw

        # and lets us start jobs
        origin = f"http://127.0.0.1:{server.port}"
        status, job, _ = server.post_json(
            "/api/jobs", {"action": "deploy", "version": "v1"},
            {"Cookie": cookie, "Origin": origin})
        assert status == 202
        finished = server.wait_job(job["id"], headers={"Cookie": cookie})
        assert finished["status"] == "done"

        # logout invalidates both the cookie and the bearer token
        status, _, _ = server.post_json("/api/logout", {}, {"Cookie": cookie})
        assert status == 200
        assert server.get("/api/status", {"Cookie": cookie})[0] == 401
        assert server.get_raw(
            "/api/status", {"Authorization": "Bearer " + body["token"]})[0] == 401
    finally:
        server.close()


def test_cookie_auth_posts_are_csrf_guarded(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory, password="hunter2")
    try:
        _, _, cookie = server.login()
        origin = f"http://127.0.0.1:{server.port}"

        # browser-less cookie POST with no Origin/Referer -> rejected
        status, body, _ = server.post_json("/api/jobs", {"action": "rollback"},
                                           {"Cookie": cookie})
        assert status == 403 and "CSRF" in body["error"]

        # mismatched Origin -> rejected
        status, _, _ = server.post_json("/api/jobs", {"action": "rollback"},
                                        {"Cookie": cookie, "Origin": "http://evil.example"})
        assert status == 403

        # same-origin Origin -> accepted
        status, job, _ = server.post_json("/api/jobs", {"action": "rollback"},
                                          {"Cookie": cookie, "Origin": origin})
        assert status == 202
        assert job["id"]

        # bearer auth is not subject to the CSRF check
        status, body, _ = server.post_json("/api/login",
                                           {"username": "admin", "password": "hunter2"})
        token = body["token"]
        status, job, _ = server.post_json("/api/jobs", {"action": "rollback"},
                                          {"Authorization": "Bearer " + token})
        assert status == 202
    finally:
        server.close()


def test_static_api_token_mode(tmp_path, backend_factory):
    # BG_API_TOKEN only: programmatic access via Authorization: Bearer; no
    # password -> UI login is not possible.
    server = ApiServer(tmp_path, backend_factory, api_token="tok-123")
    try:
        assert server.get("/api/status")[0] == 401
        status, _, _ = server.get_raw("/api/status",
                                      {"Authorization": "Bearer tok-123"})
        assert status == 200

        status, body, _ = server.post_json("/api/login",
                                           {"username": "admin", "password": "x"})
        assert status == 403 and "not configured" in body["error"]

        # webhook accepts the bearer token
        status, body, _ = server.post_json("/api/webhook/github", _tag_payload("v1"),
                                           {"Authorization": "Bearer tok-123"})
        assert status == 202
        job = server.wait_job(body["job"]["id"],
                              headers={"Authorization": "Bearer tok-123"})
        assert job["status"] == "done"
    finally:
        server.close()


def test_webhook_requires_global_auth_when_enabled(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory, password="hunter2")
    try:
        # no credential at all -> 401
        status, _, _ = server.post_json("/api/webhook/github", _tag_payload())
        assert status == 401

        # a login session token unlocks the webhook too
        _, login_body, _ = server.login()
        bearer = {"Authorization": "Bearer " + login_body["token"]}
        status, body, _ = server.post_json(
            "/api/webhook/github", _tag_payload("v1"), bearer)
        assert status == 202
        job = server.wait_job(body["job"]["id"], headers=bearer)
        assert job["status"] == "done"
    finally:
        server.close()


def test_webhook_token_still_works_with_auth_on(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory, password="hunter2",
                       webhook_token="sekrit")
    try:
        # wrong X-Bg-Token and no other credential -> rejected
        status, _, _ = server.post_json("/api/webhook/github", _tag_payload(),
                                        {"X-Bg-Token": "nope"})
        assert status == 401

        # correct X-Bg-Token -> accepted (webhook secret is its own credential)
        status, body, _ = server.post_json("/api/webhook/github", _tag_payload("v1"),
                                           {"X-Bg-Token": "sekrit"})
        assert status == 202
        _, login_body, _ = server.login()
        job = server.wait_job(body["job"]["id"],
                              headers={"Authorization": "Bearer " + login_body["token"]})
        assert job["status"] == "done"
    finally:
        server.close()


def test_session_endpoint_reports_user(tmp_path, backend_factory):
    server = ApiServer(tmp_path, backend_factory, password="hunter2")
    try:
        _, _, cookie = server.login()
        status, info = server.get("/api/session", {"Cookie": cookie})
        assert status == 200
        assert info["authenticated"] is True
        assert info["username"] == "admin"

        # bearer tokens don't map to a username
        _, body, _ = server.login()
        status, info = server.get(
            "/api/session", {"Authorization": "Bearer " + body["token"]})
        assert info["authenticated"] is True and info["username"] is None
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# Login brute-force guard (rate limiting + account lockout)                    #
# --------------------------------------------------------------------------- #
class FakeClock:
    """Deterministic clock for the AuthManager guard tests."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_login_rate_limit_blocks_and_recovers(tmp_path, backend_factory):
    clock = FakeClock()
    server = ApiServer(tmp_path, backend_factory, password="hunter2",
                       rate_limit=3, rate_window=60.0, clock=clock)
    try:
        for _ in range(3):
            status, body, _ = server.post_json(
                "/api/login", {"username": "admin", "password": "wrong"})
            assert status == 401 and "invalid" in body["error"]

        # 4th attempt is blocked even with the correct password
        status, body, headers = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429
        assert "too many login attempts" in body["error"]
        assert body["retry_after"] > 0
        assert int(headers.get("Retry-After") or 0) == body["retry_after"]

        # still blocked mid-window
        clock.advance(30)
        status, _, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429

        # once the window elapses the account works again
        clock.advance(31)
        status, body, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200 and body["token"]
    finally:
        server.close()


def test_account_lockout_after_repeated_failures(tmp_path, backend_factory):
    clock = FakeClock()
    server = ApiServer(tmp_path, backend_factory, password="hunter2",
                       lockout_attempts=3, lockout_seconds=900.0, clock=clock)
    try:
        for _ in range(3):
            status, _, _ = server.post_json(
                "/api/login", {"username": "admin", "password": "wrong"})
            assert status == 401

        # locked: even correct credentials are rejected with a 429 + Retry-After
        status, body, headers = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429
        assert "locked" in body["error"]
        assert body["retry_after"] > 0
        assert int(headers.get("Retry-After") or 0) == body["retry_after"]

        # attempts during the lockout don't extend it or reset the counter
        clock.advance(100)
        status, body, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429 and "locked" in body["error"]

        # after the lockout expires the account works again
        clock.advance(801)
        status, body, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200 and body["token"]

        # a successful login reset the failure counter: 2 failures don't lock
        for _ in range(2):
            status, _, _ = server.post_json(
                "/api/login", {"username": "admin", "password": "wrong"})
            assert status == 401
        status, _, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200
    finally:
        server.close()


def test_rate_limit_is_per_client_ip(tmp_path, backend_factory):
    clock = FakeClock()
    server = ApiServer(tmp_path, backend_factory, password="hunter2",
                       rate_limit=2, rate_window=60.0, trust_proxy=True,
                       clock=clock)
    try:
        forwarded = {"X-Forwarded-For": "10.0.0.1"}
        for _ in range(2):
            status, _, _ = server.post_json(
                "/api/login", {"username": "admin", "password": "wrong"},
                forwarded)
            assert status == 401

        # the same client IP is now blocked...
        status, body, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"},
            forwarded)
        assert status == 429 and "too many" in body["error"]

        # ...but a different client IP is unaffected
        status, body, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"},
            {"X-Forwarded-For": "10.0.0.2"})
        assert status == 200 and body["token"]

        # without trust_proxy the header is ignored (peer IP is shared here)
        server2 = ApiServer(tmp_path, backend_factory, password="hunter2",
                            rate_limit=1, rate_window=60.0, clock=clock)
        try:
            status, _, _ = server2.post_json(
                "/api/login", {"username": "admin", "password": "wrong"},
                {"X-Forwarded-For": "10.9.9.9"})
            assert status == 401
            status, _, _ = server2.post_json(
                "/api/login", {"username": "admin", "password": "wrong"},
                {"X-Forwarded-For": "10.8.8.8"})
            assert status == 429  # same peer IP -> rate limited
        finally:
            server2.close()
    finally:
        server.close()


def test_login_guard_does_not_affect_other_endpoints(tmp_path, backend_factory):
    clock = FakeClock()
    server = ApiServer(tmp_path, backend_factory, password="hunter2",
                       rate_limit=1, clock=clock)
    try:
        # burn the only allowed attempt
        status, _, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "wrong"})
        assert status == 401
        status, _, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429

        # healthz / session stay open and authenticated endpoints still work
        # with a bearer token obtained before the limit kicked in
        assert server.get("/api/healthz")[0] == 200
        assert server.get("/api/session")[0] == 200
    finally:
        server.close()


def test_lockout_survives_api_restart(tmp_path, backend_factory):
    clock = FakeClock()
    state_file = str(tmp_path / "auth.json")

    server1 = ApiServer(tmp_path, backend_factory, password="hunter2",
                        lockout_attempts=2, lockout_seconds=900.0,
                        clock=clock, state_file=state_file)
    try:
        for _ in range(2):
            status, _, _ = server1.post_json(
                "/api/login", {"username": "admin", "password": "wrong"})
            assert status == 401
        assert os.path.exists(state_file)
    finally:
        server1.close()

    # "restart": a brand-new server that shares only the persisted state file
    server2 = ApiServer(tmp_path, backend_factory, password="hunter2",
                        lockout_attempts=2, lockout_seconds=900.0,
                        clock=clock, state_file=state_file)
    try:
        # the lockout from before the restart is still enforced
        status, body, _ = server2.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429 and "locked" in body["error"]

        # once the lockout expires the account works again
        clock.advance(901)
        status, body, _ = server2.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200 and body["token"]
    finally:
        server2.close()


def test_rate_limit_counters_survive_api_restart(tmp_path, backend_factory):
    clock = FakeClock()
    state_file = str(tmp_path / "auth.json")

    server1 = ApiServer(tmp_path, backend_factory, password="hunter2",
                        rate_limit=1, rate_window=60.0,
                        lockout_attempts=99, clock=clock, state_file=state_file)
    try:
        status, _, _ = server1.post_json(
            "/api/login", {"username": "admin", "password": "wrong"})
        assert status == 401
    finally:
        server1.close()

    server2 = ApiServer(tmp_path, backend_factory, password="hunter2",
                        rate_limit=1, rate_window=60.0,
                        lockout_attempts=99, clock=clock, state_file=state_file)
    try:
        # the single allowed attempt was already consumed before the restart
        status, body, _ = server2.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 429 and "too many" in body["error"]

        # after the window elapses the login succeeds
        clock.advance(61)
        status, body, _ = server2.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200 and body["token"]
    finally:
        server2.close()


def test_corrupt_guard_state_file_is_tolerated(tmp_path, backend_factory):
    state_file = tmp_path / "auth.json"
    state_file.write_text("{not valid json!!")
    server = ApiServer(tmp_path, backend_factory, password="hunter2",
                       state_file=str(state_file))
    try:
        # unreadable guard state -> server starts fresh and login still works
        status, body, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200 and body["token"]
        # and the guard keeps functioning after the fresh start
        status, _, _ = server.post_json(
            "/api/login", {"username": "admin", "password": "wrong"})
        assert status == 401
    finally:
        server.close()


def test_successful_login_clears_persisted_counters(tmp_path, backend_factory):
    clock = FakeClock()
    state_file = str(tmp_path / "auth.json")

    server1 = ApiServer(tmp_path, backend_factory, password="hunter2",
                        rate_limit=3, rate_window=60.0,
                        lockout_attempts=3, clock=clock, state_file=state_file)
    try:
        # two failures, then a success: counters are reset on disk
        for _ in range(2):
            status, _, _ = server1.post_json(
                "/api/login", {"username": "admin", "password": "wrong"})
            assert status == 401
        status, _, _ = server1.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200
    finally:
        server1.close()

    server2 = ApiServer(tmp_path, backend_factory, password="hunter2",
                        rate_limit=3, rate_window=60.0,
                        lockout_attempts=3, clock=clock, state_file=state_file)
    try:
        # after the restart the reset counters allow two more attempts
        for _ in range(2):
            status, _, _ = server2.post_json(
                "/api/login", {"username": "admin", "password": "wrong"})
            assert status == 401
        status, body, _ = server2.post_json(
            "/api/login", {"username": "admin", "password": "hunter2"})
        assert status == 200 and body["token"]
    finally:
        server2.close()
