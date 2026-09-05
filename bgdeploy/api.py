"""HTTP control API + web UI.

Exposes bgdeploy's deploy / rollback / status as a small HTTP service (stdlib
only). Deploys and rollbacks take minutes (image builds, smoke tests, gradual
traffic ramps), so they run as background *jobs* that the UI and clients poll:

    GET  /api/status        deployment snapshot (same as `bgdeploy status`)
    POST /api/jobs          start a job: {"action": "deploy"|"rollback", "version": ...}
    GET  /api/jobs          recent jobs
    GET  /api/jobs/{id}     one job incl. its live log
    POST /api/webhook/github   GitHub push/tag webhook -> tags auto-start deploys
    GET  /api/healthz       API liveness (no engine needed)
    GET  /api/session       current auth state (authenticated?/username)
    POST /api/login         {username, password} -> session token + HttpOnly cookie
    POST /api/logout        invalidate the session
    GET  /                   embedded web UI with a one-click rollback button

Authentication is off by default; set BG_API_PASSWORD (UI login + sessions)
and/or BG_API_TOKEN (static bearer for API clients) to enable it. When on, all
endpoints except /api/healthz, /api/session, /api/login and the login page
require a valid session cookie or `Authorization: Bearer <token>`, and
cookie-authenticated POSTs are CSRF-guarded via the Origin/Referer header.

Login is brute-force protected: per-client-IP rate limiting (BG_LOGIN_RATE_*
and BG_API_TRUST_PROXY for X-Forwarded-For) plus per-username account lockout
(BG_LOGIN_LOCKOUT_*) that rejects further attempts with 429 + Retry-After.

The deployer's file lock keeps deploys/rollbacks single-flight even when
multiple jobs (or a human on the CLI) race.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

from .deployer import BlueGreenDeployer, DeploymentError
from .engine import EngineError, select_engine
from .state import DeploymentLockedError
from .traffic import fetch_router_health

MAX_JOBS = 100
INDEX_HTML = None  # set below
LOGIN_HTML = None  # set below
SESSION_COOKIE = "bgdeploy_session"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_bytes(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


class AuthManager:
    """Login sessions + static bearer token for the control API/UI.

    Auth is off when neither a password nor a token is configured (local
    convenience). When enabled:
      - POST /api/login {username, password} -> opaque session token + cookie
      - API clients authenticate with `Authorization: Bearer <token>` where the
        token is a login session token or the static `BG_API_TOKEN`
      - the web UI logs in through the login page; the HttpOnly session cookie
        is sent on same-origin requests and POSTs are CSRF-guarded
    """

    def __init__(self, username: str = "admin", password: str = "",
                 token: str = "", session_ttl: float = 43200.0,
                 rate_limit: int = 5, rate_window: float = 60.0,
                 lockout_attempts: int = 5, lockout_seconds: float = 900.0,
                 trust_proxy: bool = False, clock=None,
                 state_file: str | None = None):
        self.username = username
        self.password = password
        self.token = token
        self.session_ttl = session_ttl
        self.rate_limit = rate_limit          # /api/login attempts per client/window
        self.rate_window = rate_window
        self.lockout_attempts = lockout_attempts  # consecutive failures to lock
        self.lockout_seconds = lockout_seconds    # account lockout duration
        self.trust_proxy = trust_proxy            # use X-Forwarded-For for the client IP
        self.state_file = state_file              # persist guard state here (best-effort)
        self._clock = clock or time.time
        self._sessions: dict[str, list] = {}  # token -> [username, expiry]
        self._attempts: dict[str, list] = {}  # client key -> attempt timestamps
        self._failures: dict[str, int] = {}   # username -> consecutive failures
        self._lockouts: dict[str, float] = {} # username -> locked until (epoch)
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------- guard state persistence ------
    def _load(self) -> None:
        """Restore attempt/lockout state from disk (pruned against the clock).
        Unreadable or malformed files are ignored -> start fresh."""
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        now = self._clock()
        attempts = data.get("attempts")
        if isinstance(attempts, dict):
            for client, stamps in attempts.items():
                if not isinstance(stamps, list):
                    continue
                kept = [t for t in stamps
                        if isinstance(t, (int, float)) and now - t < self.rate_window]
                if kept:
                    self._attempts[str(client)] = kept
        failures = data.get("failures")
        if isinstance(failures, dict):
            self._failures = {str(u): int(c) for u, c in failures.items()
                              if isinstance(c, (int, float)) and not isinstance(c, bool)}
        lockouts = data.get("lockouts")
        if isinstance(lockouts, dict):
            self._lockouts = {str(u): float(t) for u, t in lockouts.items()
                              if isinstance(t, (int, float)) and not isinstance(t, bool)
                              and t > now}

    def _save(self) -> None:
        """Atomically write the guard state (caller holds self._lock)."""
        if not self.state_file:
            return
        payload = {"attempts": self._attempts, "failures": self._failures,
                   "lockouts": self._lockouts}
        try:
            directory = os.path.dirname(self.state_file) or "."
            os.makedirs(directory, exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.state_file)
        except OSError:
            pass  # guard state is best-effort; never break logins over a disk issue

    # ----------------------------------------------- login brute-force guard ----
    def rate_status(self, client: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds) for this client within the window."""
        now = self._clock()
        with self._lock:
            stamps = [t for t in self._attempts.get(client, [])
                      if now - t < self.rate_window]
            if len(stamps) != len(self._attempts.get(client, [])):
                self._attempts[client] = stamps
                self._save()
            if len(stamps) >= self.rate_limit:
                return False, max(1.0, self.rate_window - (now - stamps[0]))
            return True, 0.0

    def lockout_remaining(self, username: str) -> float:
        with self._lock:
            return max(0.0, self._lockouts.get(username, 0.0) - self._clock())

    def record_failure(self, username: str, client: str) -> None:
        """Count a failed login; locks the account at the failure threshold."""
        now = self._clock()
        with self._lock:
            stamps = [t for t in self._attempts.get(client, [])
                      if now - t < self.rate_window]
            stamps.append(now)
            self._attempts[client] = stamps
            failures = self._failures.get(username, 0) + 1
            if failures >= self.lockout_attempts:
                self._lockouts[username] = now + self.lockout_seconds
                self._failures[username] = 0
            else:
                self._failures[username] = failures
            self._save()

    def record_success(self, username: str, client: str) -> None:
        """A successful login resets the failure and attempt counters."""
        with self._lock:
            self._failures.pop(username, None)
            self._lockouts.pop(username, None)
            self._attempts.pop(client, None)
            self._save()

    @property
    def enabled(self) -> bool:
        return bool(self.password) or bool(self.token)

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            for t in [t for t, (_, exp) in self._sessions.items() if exp <= now]:
                self._sessions.pop(t, None)
            self._sessions[token] = [username, now + self.session_ttl]
        return token

    def destroy_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def valid(self, token: str) -> bool:
        """True if token is the static API token or a live session token."""
        if not token:
            return False
        if self.token and secrets.compare_digest(token, self.token):
            return True
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return False
            if entry[1] <= time.time():
                self._sessions.pop(token, None)
                return False
        return True

    def username_for(self, token: str) -> str | None:
        with self._lock:
            entry = self._sessions.get(token)
        return entry[0] if entry else None


class ControlAPI(ThreadingHTTPServer):
    """HTTP server that owns the job registry and the deployer factory."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, make_deployer, webhook_token: str | None = None,
                 auth: AuthManager | None = None):
        super().__init__(address, _Handler)
        self.make_deployer = make_deployer  # make_deployer(log) -> deployer-like object
        self.webhook_token = webhook_token
        self.auth = auth or AuthManager()
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._counter = 0

    # ------------------------------------------------------------ jobs ----
    def submit(self, action: str, version: str | None = None,
               skip_smoke: bool = False, no_rebuild: bool = False) -> dict:
        with self._lock:
            self._counter += 1
            job_id = f"job-{self._counter}"
            job = {
                "id": job_id,
                "action": action,
                "version": version,
                "status": "queued",      # queued | running | done | failed
                "created": _now_iso(),
                "finished": None,
                "log": [],
                "result": None,
                "error": None,
            }
            self.jobs[job_id] = job
            self._trim()
        thread = threading.Thread(target=self._run_job, args=(job, action, version, skip_smoke, no_rebuild), daemon=True)
        thread.start()
        return job

    def _trim(self) -> None:
        overflow = sorted(self.jobs, key=lambda j: self.jobs[j]["created"])[:-MAX_JOBS]
        for job_id in overflow:
            self.jobs.pop(job_id, None)

    def _run_job(self, job: dict, action: str, version, skip_smoke: bool, no_rebuild: bool) -> None:
        def log_line(message: str) -> None:
            with self._lock:
                job["log"].append(message)
        job["status"] = "running"
        try:
            deployer = self.make_deployer(log=log_line)
            if action == "deploy":
                job["result"] = deployer.deploy(version=version, rebuild=not no_rebuild, skip_smoke=skip_smoke)
            elif action == "rollback":
                job["result"] = deployer.rollback(skip_smoke=skip_smoke)
            else:
                raise DeploymentError(f"unknown action {action!r}")
            job["status"] = "done"
        except (DeploymentError, EngineError, DeploymentLockedError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
            log_line(f"error: {exc}")
        except Exception as exc:  # noqa: BLE001 - never lose a job silently
            job["status"] = "failed"
            job["error"] = f"{exc}\n{traceback.format_exc()}"
            log_line(f"error: {exc}")
        finally:
            job["finished"] = _now_iso()

    def status_snapshot(self) -> dict:
        deployer = self.make_deployer(log=lambda _m: None)
        return deployer.snapshot()


class _Handler(BaseHTTPRequestHandler):
    server: ControlAPI

    def log_message(self, fmt, *args):  # silence request noise in tests/logs
        pass

    # ------------------------------------------------------------- util ----
    def _send(self, code: int, payload, content_type: str = "application/json",
              extra_headers: dict | None = None) -> None:
        body = _json_bytes(payload) if content_type == "application/json" else payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- auth ----
    def _credentials(self) -> tuple[str, str] | None:
        """Extract (kind, token) from the request; kind is 'bearer' or 'cookie'."""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[len("bearer "):].strip()
            if token:
                return ("bearer", token)
        webhook = (self.headers.get("X-Bg-Token") or "").strip()
        if webhook:
            return ("bearer", webhook)
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                return ("cookie", value)
        return None

    def _client_ip(self) -> str:
        """Client address; honors X-Forwarded-For when a trusted proxy is in front."""
        if self.server.auth.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _session_info(self) -> dict:
        creds = self._credentials()
        authed = False
        username = None
        if creds and self.server.auth.valid(creds[1]):
            authed = True
            if creds[0] == "cookie":
                username = self.server.auth.username_for(creds[1])
        return {
            "authenticated": authed,
            "auth_enabled": self.server.auth.enabled,
            "username": username,
        }

    def _require_auth(self) -> bool:
        """Send 401 and return False when auth is enabled and the request has no
        valid bearer token or session cookie."""
        if not self.server.auth.enabled:
            return True
        creds = self._credentials()
        if creds and self.server.auth.valid(creds[1]):
            return True
        self._send(401, {"error": "authentication required"},
                   extra_headers={"WWW-Authenticate": "Bearer"})
        return False

    def _csrf_ok(self) -> bool:
        """For cookie-authenticated POSTs a same-origin Origin/Referer header is
        required (browsers always send Origin on POST; curl clients use bearer
        tokens instead). Requests with neither header are rejected."""
        host = self.headers.get("Host")
        matched = False
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            try:
                netloc = urllib.parse.urlsplit(value).netloc
            except ValueError:
                return False
            if netloc != host:
                return False
            matched = True
        return matched

    def _post_guard(self) -> bool:
        """Global auth gate + CSRF check for mutating requests."""
        if not self._require_auth():
            return False
        creds = self._credentials()
        if creds and creds[0] == "cookie" and not self._csrf_ok():
            self._send(403, {"error": "CSRF check failed: cookie-authenticated POSTs need a "
                                   "same-origin Origin/Referer header (API clients: use "
                                   "Authorization: Bearer)"})
            return False
        return True

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {"_": data}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------- http ----
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            # Always serve a page: the panel for valid sessions, otherwise the
            # login page (a plain 401 would make a browser visit confusing).
            page = INDEX_HTML
            if self.server.auth.enabled:
                creds = self._credentials()
                authed = creds is not None and self.server.auth.valid(creds[1])
                page = INDEX_HTML if authed else LOGIN_HTML
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/api/healthz":
            return self._send(200, {"status": "ok"})
        if path == "/api/session":
            return self._send(200, self._session_info())
        if path == "/api/status":
            if not self._require_auth():
                return
            try:
                return self._send(200, self.server.status_snapshot())
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": str(exc)})
        if path == "/api/jobs":
            if not self._require_auth():
                return
            jobs = sorted(self.server.jobs.values(), key=lambda j: j["created"], reverse=True)
            return self._send(200, {"jobs": jobs})
        if path.startswith("/api/jobs/"):
            if not self._require_auth():
                return
            job_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            job = self.server.jobs.get(job_id)
            if job is None:
                return self._send(404, {"error": f"no such job {job_id}"})
            return self._send(200, job)
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            return self._handle_login()
        if path == "/api/logout":
            return self._handle_logout()
        if path == "/api/jobs":
            if not self._post_guard():
                return
            body = self._read_body()
            if body is None:
                return self._send(400, {"error": "request body must be JSON"})
            action = (body.get("action") or "").lower()
            if action not in ("deploy", "rollback"):
                return self._send(400, {"error": "action must be 'deploy' or 'rollback'"})
            if action == "deploy" and not (body.get("version") or "").strip():
                return self._send(400, {"error": "a version is required for deploys"})
            job = self.server.submit(
                action=action,
                version=str(body.get("version") or "").strip() or None,
                skip_smoke=bool(body.get("skip_smoke", False)),
                no_rebuild=bool(body.get("no_rebuild", False)),
            )
            return self._send(202, job)
        if path == "/api/webhook/github":
            return self._handle_github_webhook()
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------ auth ----
    def _handle_login(self):
        auth = self.server.auth
        if not auth.enabled:
            return self._send(403, {"error": "authentication is not enabled on this server"})
        body = self._read_body()
        if body is None:
            return self._send(400, {"error": "request body must be JSON"})
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not (auth.username and auth.password):
            return self._send(403, {"error": "password login is not configured (BG_API_PASSWORD)"})
        client = self._client_ip()

        # brute-force guard: account lockout first, then per-IP rate limit
        locked_for = auth.lockout_remaining(username)
        if locked_for > 0:
            retry = int(locked_for)
            return self._send(429, {"error": f"account locked; try again in {retry}s",
                                    "retry_after": retry},
                              extra_headers={"Retry-After": str(retry)})
        allowed, retry_after = auth.rate_status(client)
        if not allowed:
            retry = int(retry_after)
            return self._send(429, {"error": "too many login attempts; slow down",
                                    "retry_after": retry},
                              extra_headers={"Retry-After": str(retry)})

        if not (secrets.compare_digest(username, auth.username)
                and secrets.compare_digest(password, auth.password)):
            auth.record_failure(username, client)
            return self._send(401, {"error": "invalid username or password"})
        auth.record_success(username, client)
        token = auth.create_session(username)
        cookie = (f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; "
                  f"Max-Age={int(auth.session_ttl)}")
        return self._send(200, {"token": token, "username": username,
                                "expires": _now_iso()},
                          extra_headers={"Set-Cookie": cookie})

    def _handle_logout(self):
        creds = self._credentials()
        if creds and creds[0] == "cookie":
            self.server.auth.destroy_session(creds[1])
        clear = f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
        return self._send(200, {"ok": True}, extra_headers={"Set-Cookie": clear})

    # ------------------------------------------------------- webhook -------
    def _handle_github_webhook(self):
        # Accept a valid X-Bg-Token (webhook secret) as the credential; otherwise
        # fall back to the global auth scheme (bearer token / session cookie).
        provided = (self.headers.get("X-Bg-Token") or "").strip()
        webhook_ok = bool(self.server.webhook_token) and bool(provided) and \
            secrets.compare_digest(provided, self.server.webhook_token)
        if not webhook_ok:
            if not self._post_guard():
                return
            if self.server.webhook_token and not self.server.auth.enabled:
                return self._send(401, {"error": "bad or missing X-Bg-Token"})
        body = self._read_body()
        if body is None:
            return self._send(400, {"error": "request body must be JSON"})
        ref = str(body.get("ref") or "")
        deleted = bool(body.get("deleted", False))
        if not ref.startswith("refs/tags/"):
            return self._send(200, {"triggered": False, "reason": f"{ref!r} is not a tag push"})
        if deleted:
            return self._send(200, {"triggered": False, "reason": "tag deletion ignored"})
        version = ref[len("refs/tags/"):]
        if not version:
            return self._send(200, {"triggered": False, "reason": "empty tag"})
        # GitHub delivers a "ping" payload on first webhook setup - ignore it.
        if body.get("zen"):
            return self._send(200, {"triggered": False, "reason": "ping"})
        job = self.server.submit(action="deploy", version=version)
        return self._send(202, {"triggered": True, "job": job})

    # ------------------------------------------------------------ misc ----
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()


def create_control_server(cfg, host: str = "127.0.0.1", port: int = 0,
                          webhook_token: str | None = None) -> ControlAPI:
    """Build a server around the real BlueGreenDeployer (used by the CLI)."""
    engine = select_engine(cfg.engine)
    auth = AuthManager(username=cfg.api_user, password=cfg.api_password,
                       token=cfg.api_token, session_ttl=cfg.session_ttl,
                       rate_limit=cfg.login_rate_limit,
                       rate_window=cfg.login_rate_window,
                       lockout_attempts=cfg.login_lockout_attempts,
                       lockout_seconds=cfg.login_lockout_seconds,
                       trust_proxy=cfg.trust_proxy,
                       state_file=cfg.auth_state_file)

    def make_deployer(log):
        return BlueGreenDeployer(
            cfg,
            engine=engine,
            router_checker=fetch_router_health,
            log=log,
        )

    return ControlAPI((host, port), make_deployer, webhook_token=webhook_token,
                      auth=auth)


# --------------------------------------------------------------------------- #
# Web UI: login page (served when auth is on and no session cookie)            #
# --------------------------------------------------------------------------- #
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>bgdeploy · sign in</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0;
         background: #101418; color: #d6dee6; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; }
  .card { background: #161c22; border: 1px solid #26303a; border-radius: 10px;
          padding: 28px 32px; width: 300px; }
  h1 { font-size: 16px; margin: 0 0 4px; color: #fff; }
  p { color: #7f8b98; font-size: 12px; margin: 0 0 18px; }
  label { display: block; font-size: 11px; text-transform: uppercase;
          letter-spacing: .08em; color: #7f8b98; margin: 12px 0 4px; }
  input { width: 100%; box-sizing: border-box; background: #0c1014;
          border: 1px solid #2a3540; color: #d6dee6; border-radius: 6px;
          padding: 9px; font: inherit; }
  button { width: 100%; margin-top: 18px; background: #1d6fe0; color: #fff;
           border: 0; border-radius: 6px; padding: 10px; font: inherit;
           cursor: pointer; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  #error { color: #f87171; font-size: 12px; min-height: 16px; margin-top: 12px; }
</style>
</head>
<body>
  <div class="card">
    <h1>bgdeploy</h1>
    <p>blue/green control panel · sign in to continue</p>
    <form id="login-form">
      <label for="username">username</label>
      <input id="username" autocomplete="username" required>
      <label for="password">password</label>
      <input id="password" type="password" autocomplete="current-password" required>
      <button type="submit" id="submit">Sign in</button>
    </form>
    <div id="error"></div>
  </div>
<script>
const $ = (id) => document.getElementById(id);
const err = $("error");
$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  err.textContent = "";
  $("submit").disabled = true;
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("username").value,
                             password: $("password").value }),
    });
    const data = await r.json();
    if (r.status === 200) { location.href = "/"; return; }
    err.textContent = data.error || "login failed";
    if (data.retry_after) err.textContent += " (retry in " + data.retry_after + "s)";
  } catch (e) { err.textContent = "login failed: " + e; }
  $("submit").disabled = false;
});
</script>
</body>
</html>
"""

# --------------------------------------------------------------------------- #
# Web UI (single self-contained page)                                          #
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>bgdeploy control panel</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0;
         background: #101418; color: #d6dee6; }
  header { padding: 14px 22px; border-bottom: 1px solid #26303a; display: flex;
           align-items: baseline; gap: 14px; }
  header h1 { font-size: 16px; margin: 0; color: #fff; }
  header span { color: #7f8b98; font-size: 12px; }
  main { padding: 22px; display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  section { border: 1px solid #26303a; border-radius: 8px; padding: 16px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
       color: #7f8b98; margin: 0 0 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { text-align: left; padding: 5px 8px; border-bottom: 1px solid #1c242d; }
  .ok { color: #4ade80; } .bad { color: #f87171; } .muted { color: #7f8b98; }
  button { background: #1d6fe0; color: #fff; border: 0; border-radius: 6px;
           padding: 8px 14px; font: inherit; cursor: pointer; }
  button.rollback { background: #c2410c; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  input { background: #0c1014; border: 1px solid #2a3540; color: #d6dee6;
          border-radius: 6px; padding: 8px; font: inherit; width: 240px; }
  form { display: flex; gap: 8px; align-items: center; }
  pre { background: #0c1014; border: 1px solid #1c242d; border-radius: 6px;
        padding: 10px; font-size: 12px; height: 260px; overflow: auto;
        white-space: pre-wrap; }
  .row { display: flex; gap: 8px; margin-top: 10px; }
  .pill { font-size: 11px; border-radius: 10px; padding: 2px 8px;
          background: #1c242d; color: #9fb0bd; }
  .pill.running { background: #1d6fe033; color: #7fb2ff; }
  .pill.done { background: #14321f; color: #4ade80; }
  .pill.failed { background: #3a1416; color: #f87171; }
</style>
</head>
<body>
<header>
  <h1>bgdeploy</h1><span>blue/green control panel</span>
  <span id="user" style="margin-left:auto; color:#7f8b98; font-size:12px"></span>
  <button id="logout" style="display:none; padding:4px 10px; font-size:12px">logout</button>
</header>
<main>
  <section>
    <h2>deploy</h2>
    <form id="deploy-form">
      <input id="version" placeholder="version, e.g. v1.2.0" required>
      <button type="submit">Deploy</button>
    </form>
    <h2 style="margin-top:20px">rollback</h2>
    <p class="muted" style="font-size:12px">Shift traffic back to the previously
       committed release (smoke-tested before switching).</p>
    <button id="rollback" class="rollback">Rollback to previous release</button>
  </section>

  <section>
    <h2>status</h2>
    <table id="status"></table>
    <h2 style="margin-top:20px">active job log</h2>
    <pre id="log">no job running</pre>
  </section>

  <section style="grid-column: 1 / -1">
    <h2>recent jobs</h2>
    <table id="jobs"></table>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let activeJob = null;

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = "/"; throw new Error("unauthorized"); }
  return r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (r.status === 401) { location.href = "/"; throw new Error("unauthorized"); }
  return r.json();
}

function esc(s) { return String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

async function refreshSession() {
  try {
    const s = await jget("/api/session");
    $("user").textContent = s.username || (s.authenticated ? "authenticated" : "");
    $("logout").style.display = s.authenticated ? "" : "none";
  } catch (e) { /* redirect already handled on 401 */ }
}
$("logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/";
});

async function refreshStatus() {
  try {
    const s = await jget("/api/status");
    const st = s.state || {};
    let html = "";
    html += row("engine", s.engine);
    html += row("active color", st.active_color || "-");
    html += row("active version", st.active_version || "-");
    const prev = st.previous;
    html += row("previous stable", prev ? (prev.color + " " + prev.version) : "-");
    html += row("live weights", JSON.stringify(s.weights));
    for (const c of ["blue", "green"]) {
      const i = (s.containers || {})[c] || {};
      const state = i.running ? '<span class="ok">running</span>' : '<span class="bad">stopped</span>';
      html += row("container " + c, state + " <span class='muted'>" + esc(i.version || "-") +
                  (i.host_port ? " · port " + i.host_port : "") + "</span>");
    }
    const incidents = (st.incidents || []).slice(0, 3);
    if (incidents.length) html += row("recent incidents",
      esc(incidents.map(i => i.kind).join(", ")));
    $("status").innerHTML = html;
  } catch (e) { $("status").innerHTML = row("error", esc(e)); }
}
function row(k, v) { return "<tr><td class='muted'>" + k + "</td><td>" + v + "</td></tr>"; }

async function refreshJobs() {
  const data = await jget("/api/jobs");
  const jobs = data.jobs || [];
  let html = "<tr><th>job</th><th>action</th><th>version</th><th>status</th>" +
             "<th>finished</th></tr>";
  for (const j of jobs.slice(0, 12)) {
    html += "<tr><td>" + esc(j.id) + "</td><td>" + esc(j.action) +
            "</td><td>" + esc(j.version || "-") +
            "</td><td><span class='pill " + esc(j.status) + "'>" + esc(j.status) +
            "</span></td><td class='muted'>" + esc((j.finished || "").slice(11, 19)) + "</td></tr>";
  }
  $("jobs").innerHTML = html;
  const running = jobs.find(j => j.status === "running" || j.status === "queued");
  activeJob = running ? running.id : null;
}

async function refreshLog() {
  if (!activeJob) { $("log").textContent = "no job running"; return; }
  const j = await jget("/api/jobs/" + activeJob);
  $("log").textContent = (j.log || []).join("\\n") || "(no output yet)";
  if (j.status === "done") $("log").textContent += "\\n\\n[job " + j.id + " finished ok]";
  if (j.status === "failed") $("log").textContent += "\\n\\n[job " + j.id + " failed: " + j.error + "]";
}

async function watch(job) {
  activeJob = job.id;
  $("log").textContent = "starting " + job.action + "...";
  for (let i = 0; i < 900; i++) {
    await new Promise(r => setTimeout(r, 1000));
    const j = await jget("/api/jobs/" + job.id);
    $("log").textContent = (j.log || []).join("\\n") || "(working...)";
    if (j.status === "done" || j.status === "failed") {
      if (j.status === "done") $("log").textContent += "\\n\\n[job " + j.id + " finished ok]";
      else $("log").textContent += "\\n\\n[job " + j.id + " failed: " + (j.error || "") + "]";
      break;
    }
  }
  refreshStatus(); refreshJobs();
}

$("deploy-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const version = $("version").value.trim();
  if (!version) return;
  $("version").value = "";
  const job = await jpost("/api/jobs", { action: "deploy", version });
  if (job.error) { $("log").textContent = "error: " + job.error; return; }
  await watch(job);
});

$("rollback").addEventListener("click", async () => {
  if (!confirm("Shift all traffic back to the previous release?")) return;
  $("rollback").disabled = true;
  try {
    const job = await jpost("/api/jobs", { action: "rollback" });
    if (job.error) { $("log").textContent = "error: " + job.error; return; }
    await watch(job);
  } finally { $("rollback").disabled = false; }
});

refreshStatus(); refreshJobs(); refreshLog(); refreshSession();
setInterval(refreshStatus, 3000);
setInterval(refreshJobs, 3000);
setInterval(refreshLog, 2000);
</script>
</body>
</html>
"""
