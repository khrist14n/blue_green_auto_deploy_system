"""Runtime configuration for bgdeploy.

Every value can be overridden through BG_* environment variables so the same
code runs against docker or podman without changes:

    BG_ENGINE=podman            container engine (docker|podman|auto)
    BG_DATA_DIR=.bgdeploy       where state.json / weights.json live
    BG_NETWORK=bgdeploy         docker/podman network shared by the stack
    BG_IMAGE=bgdeploy/service   image name used for the app (version becomes the tag)
    BG_SERVICE_DIR=service      build context of the app image
    BG_ROUTER_IMAGE=bgdeploy/router
    BG_ROUTER_PORT=8080         host port published by the router
    BG_SMOKE_FILE=smoke-tests.json
    BG_SHIFT_STEPS=25,50,75,100 gradual traffic-shift ramp
    ...
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

COLORS = ("blue", "green")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_steps(name: str, default: tuple) -> tuple:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(int(x) for x in raw.split(",") if x.strip() != "")


@dataclass
class Config:
    # engine
    engine: str = "auto"  # docker | podman | auto
    # containers / network
    network: str = "bgdeploy"
    container_prefix: str = "app"
    replicas: int = 1  # app containers per color; >1 enables per-instance health/HA
    image: str = "bgdeploy/service"
    service_dir: str = "service"
    service_dockerfile: str = "Dockerfile"
    router_image: str = "bgdeploy/router"
    router_dir: str = "router"
    router_name: str = "bgdeploy-router"
    app_container_port: int = 8000
    app_health_path: str = "/healthz"
    publish_bind: str = "127.0.0.1"
    router_bind: str = "127.0.0.1"
    router_host_port: int = 8080
    router_internal_port: int = 8080
    router_health_interval: float = 2.0
    # data
    data_dir: str = ".bgdeploy"
    smoke_file: str = "smoke-tests.json"
    # timing
    smoke_timeout: float = 5.0
    health_wait_timeout: float = 120.0
    health_poll: float = 2.0
    shift_steps: tuple = field(default_factory=lambda: (25, 50, 75, 100))
    shift_dwell: float = 3.0
    verify_samples: int = 12
    # lifecycle
    keep_old: bool = True
    app_extra_env: str = ""  # comma-separated K=V pairs injected into app containers
    # auto-recovery
    check_interval: float = 5.0
    fail_threshold: int = 3
    restart_cooldown: float = 120.0
    auto_restore: bool = True
    restore_steady: int = 3
    # alerting
    alert_webhook: str = ""  # POST JSON {kind, detail, ...} on recovery events
    alert_min_interval: float = 60.0
    # control API auth (login + session/token)
    api_user: str = "admin"
    api_password: str = ""  # when set, the API/UI require login (GET / serves a login page)
    api_token: str = ""     # static bearer token accepted by the API (Authorization: Bearer)
    session_ttl: float = 43200.0  # seconds a login session stays valid (default 12h)
    # login brute-force protection
    login_rate_limit: int = 5  # allowed /api/login attempts per client IP per window
    login_rate_window: float = 60.0
    login_lockout_attempts: int = 5  # consecutive failures before locking the account
    login_lockout_seconds: float = 900.0  # account lockout duration (default 15 min)
    trust_proxy: bool = False  # honor X-Forwarded-For to identify the client IP

    # derived paths
    @property
    def state_file(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    @property
    def weights_file(self) -> str:
        return os.path.join(self.data_dir, "weights.json")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.data_dir, "deploy.lock")

    @property
    def auth_state_file(self) -> str:
        """Login guard state (attempt counters + lockouts) persisted across API
        restarts. Sessions stay in memory and are dropped on restart."""
        return os.path.join(self.data_dir, "auth.json")

    def container_name(self, color: str, index: int = 0) -> str:
        """Container name for a color. With replicas > 1, instances are
        suffixed -1..-N and index 0 returns the primary instance name."""
        if self.replicas <= 1:
            return f"{self.container_prefix}-{color}"
        instance = index if index > 0 else 1
        return f"{self.container_prefix}-{color}-{instance}"

    def container_names(self, color: str) -> list[str]:
        """All instance container names for a color."""
        if self.replicas <= 1:
            return [self.container_name(color)]
        return [self.container_name(color, i) for i in range(1, self.replicas + 1)]

    def image_tag(self, version: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in version)
        return f"{self.image}:{safe}"

    def router_admin_url(self) -> str:
        return f"http://127.0.0.1:{self.router_host_port}"

    def with_overrides(self, **kw) -> "Config":
        return replace(self, **kw)

    @classmethod
    def from_env(cls) -> "Config":
        c = cls(
            engine=os.environ.get("BG_ENGINE", "auto"),
            network=os.environ.get("BG_NETWORK", "bgdeploy"),
            container_prefix=os.environ.get("BG_CONTAINER_PREFIX", "app"),
            replicas=max(1, _env_int("BG_REPLICAS", 1)),
            image=os.environ.get("BG_IMAGE", "bgdeploy/service"),
            service_dir=os.environ.get("BG_SERVICE_DIR", "service"),
            service_dockerfile=os.environ.get("BG_SERVICE_DOCKERFILE", "Dockerfile"),
            router_image=os.environ.get("BG_ROUTER_IMAGE", "bgdeploy/router"),
            router_dir=os.environ.get("BG_ROUTER_DIR", "router"),
            router_name=os.environ.get("BG_ROUTER_NAME", "bgdeploy-router"),
            app_container_port=_env_int("BG_APP_PORT", 8000),
            app_health_path=os.environ.get("BG_HEALTH_PATH", "/healthz"),
            publish_bind=os.environ.get("BG_PUBLISH_BIND", "127.0.0.1"),
            router_bind=os.environ.get("BG_ROUTER_BIND", "127.0.0.1"),
            router_host_port=_env_int("BG_ROUTER_PORT", 8080),
            router_internal_port=_env_int("BG_ROUTER_INTERNAL_PORT", 8080),
            router_health_interval=_env_float("BG_ROUTER_HEALTH_INTERVAL", 2.0),
            data_dir=os.environ.get("BG_DATA_DIR", ".bgdeploy"),
            smoke_file=os.environ.get("BG_SMOKE_FILE", "smoke-tests.json"),
            smoke_timeout=_env_float("BG_SMOKE_TIMEOUT", 5.0),
            health_wait_timeout=_env_float("BG_HEALTH_WAIT", 120.0),
            health_poll=_env_float("BG_HEALTH_POLL", 2.0),
            shift_steps=_env_steps("BG_SHIFT_STEPS", (25, 50, 75, 100)),
            shift_dwell=_env_float("BG_SHIFT_DWELL", 3.0),
            verify_samples=_env_int("BG_VERIFY_SAMPLES", 12),
            keep_old=_env_bool("BG_KEEP_OLD", True),
            app_extra_env=os.environ.get("BG_APP_EXTRA_ENV", ""),
            check_interval=_env_float("BG_CHECK_INTERVAL", 5.0),
            fail_threshold=_env_int("BG_FAIL_THRESHOLD", 3),
            restart_cooldown=_env_float("BG_RESTART_COOLDOWN", 120.0),
            auto_restore=_env_bool("BG_AUTO_RESTORE", True),
            restore_steady=_env_int("BG_RESTORE_STEADY", 3),
            alert_webhook=os.environ.get("BG_ALERT_WEBHOOK_URL", ""),
            alert_min_interval=_env_float("BG_ALERT_MIN_INTERVAL", 60.0),
            api_user=os.environ.get("BG_API_USER", "admin"),
            api_password=os.environ.get("BG_API_PASSWORD", ""),
            api_token=os.environ.get("BG_API_TOKEN", ""),
            session_ttl=_env_float("BG_API_SESSION_TTL", 43200.0),
            login_rate_limit=max(1, _env_int("BG_LOGIN_RATE_LIMIT", 5)),
            login_rate_window=_env_float("BG_LOGIN_RATE_WINDOW", 60.0),
            login_lockout_attempts=max(1, _env_int("BG_LOGIN_LOCKOUT_ATTEMPTS", 5)),
            login_lockout_seconds=_env_float("BG_LOGIN_LOCKOUT_SECONDS", 900.0),
            trust_proxy=_env_bool("BG_API_TRUST_PROXY", False),
        )
        return c
