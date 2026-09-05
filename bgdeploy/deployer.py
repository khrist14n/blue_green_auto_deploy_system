"""The blue/green deployment pipeline.

Lifecycle of one deploy (all orchestrated here, in code):

    1. pick the inactive color as the target
    2. build the service image tagged with the release version
    3. replace the target color's container with the new version
    4. wait until it is healthy, then run the automated smoke suite
       -> a broken release is removed here and never receives traffic
    5. shift traffic gradually (25 -> 50 -> 75 -> 100 %)
    6. verify from the client side that the router really serves the new color
       -> if verification fails, traffic is automatically shifted back
    7. commit the new color as active; keep the old color warm for rollback

`rollback()` flips traffic back to the previously committed color/version and
re-runs smoke tests before committing - the "single click" undo.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .config import COLORS, Config
from .engine import ContainerEngine, select_engine
from .smoke import SmokeReport, http_ok, load_cases, run_smoke, substitute
from .state import FileLock, StateStore
from .traffic import TrafficWeightsStore, sample_served


class DeploymentError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def opposite(color: str) -> str:
    return "green" if color == "blue" else "blue"


class BlueGreenDeployer:
    """Orchestrates deploys / rollbacks against docker or podman.

    Test seams (all optional): pass a fake ``engine``, a temp ``state_store``
    and ``weights``, a fake ``sampler`` (returns {"counts":..., "errors":...})
    and ``sleep``/``log``. Defaults talk to the real engine and router.
    """

    def __init__(
        self,
        cfg: Config,
        engine: ContainerEngine | None = None,
        state_store: StateStore | None = None,
        weights: TrafficWeightsStore | None = None,
        sampler=None,          # sampler(router_url, samples) -> {"counts","errors"}
        router_checker=None,   # router_checker(router_url) -> /__router/health dict; None = skip readiness wait
        sleep=time.sleep,
        log=print,
    ):
        self.cfg = cfg
        self.engine = engine or select_engine(cfg.engine)
        self.state_store = state_store or StateStore(cfg.state_file)
        self.weights = weights or TrafficWeightsStore(cfg.weights_file)
        self._sampler = sampler or sample_served
        self._router_checker = router_checker
        self._sleep = sleep
        self._log = log

    # ------------------------------------------------------------------ #
    # plumbing                                                            #
    # ------------------------------------------------------------------ #
    def _log_info(self, message: str) -> None:
        self._log(f"[bgdeploy] {message}")

    def _router_url(self) -> str:
        return self.cfg.router_admin_url()

    def _sample(self, samples: int | None = None) -> dict:
        """Sample real traffic through the router: {"counts","errors"}."""
        return self._sampler(self._router_url(), samples or self.cfg.verify_samples)

    def _verify_shift(self, expected_color: str) -> tuple[bool, dict]:
        """All sampled requests must be served by expected_color, no errors."""
        result = self._sample()
        counts = result.get("counts", {})
        ok = (
            result.get("errors", 0) == 0
            and counts.get(expected_color, 0) == self.cfg.verify_samples
        )
        return ok, result

    # ------------------------------------------------------------------ #
    # router / network                                                    #
    # ------------------------------------------------------------------ #
    def ensure_router(self) -> None:
        """Create the shared network and run the router container once."""
        cfg = self.cfg
        self.engine.network_ensure(cfg.network)

        # Weights file the router mounts must exist; prefer whatever state says
        # is active. During a durable failover that is the color traffic moved to.
        state = self.state_store.load()
        weights = {"blue": 100, "green": 0}
        target_color = state.get("active_color")
        failover = state.get("failover")
        if failover:
            target_color = failover.get("to")
        if target_color:
            weights = {"blue": 0, "green": 0}
            weights[target_color] = 100
        self.weights.set("blue", weights["blue"])  # persists the file when missing

        if not self.engine.image_exists(cfg.router_image):
            self._log_info(f"building router image {cfg.router_image}")
            self.engine.build(cfg.router_image, cfg.router_dir, "Dockerfile")

        router = cfg.router_name
        router_labels = self.engine.labels(router)
        # Recreate the router when the replica topology changed: its backend
        # lists are baked into env at start time.
        replica_mismatch = router_labels.get("bgdeploy.replicas") != str(cfg.replicas)
        if not self.engine.container_running(router) or replica_mismatch:
            self.engine.remove(router, force=True)
            data_dir_abs = os.path.abspath(cfg.data_dir)
            blue_urls = ",".join(
                f"http://{name}:{cfg.app_container_port}"
                for name in cfg.container_names("blue")
            )
            green_urls = ",".join(
                f"http://{name}:{cfg.app_container_port}"
                for name in cfg.container_names("green")
            )
            self._log_info(
                f"starting router {router} with {cfg.replicas} replica(s) per color "
                f"(published on {cfg.router_bind}:{cfg.router_host_port})"
            )
            self.engine.run_container(
                name=router,
                image=cfg.router_image,
                network=cfg.network,
                env={
                    "ROUTER_PORT": str(cfg.router_internal_port),
                    "WEIGHTS_FILE": "/bgdeploy/weights.json",
                    "BACKEND_BLUE": blue_urls,
                    "BACKEND_GREEN": green_urls,
                    "HEALTH_INTERVAL": str(cfg.router_health_interval),
                    "HEALTH_PATH": cfg.app_health_path,
                },
                labels={
                    "bgdeploy.managed": "true",
                    "bgdeploy.role": "router",
                    "bgdeploy.replicas": str(cfg.replicas),
                },
                publish_bind=cfg.router_bind,
                publish_host_port=cfg.router_host_port,
                container_port=cfg.router_internal_port,
                extra_args=["-v", f"{data_dir_abs}:/bgdeploy"],
            )

        # Wait until the router answers its own ping.
        deadline = time.monotonic() + cfg.health_wait_timeout
        while time.monotonic() < deadline:
            if http_ok(self._router_url() + "/__router/ping", cfg.smoke_timeout):
                self._log_info("router is up")
                return
            self._sleep(cfg.health_poll)
        raise DeploymentError("router did not become healthy in time")

    def _wait_router_sees_backend(self, color: str) -> None:
        """Wait until the router's health loop considers `color` healthy."""
        if self._router_checker is None:
            return
        cfg = self.cfg
        deadline = time.monotonic() + cfg.health_wait_timeout
        while time.monotonic() < deadline:
            try:
                health = self._router_checker(self._router_url())
                if health["backends"][color]["healthy"]:
                    return
            except Exception:  # noqa: BLE001 - router may still be starting up
                pass
            self._sleep(cfg.health_poll)
        raise DeploymentError(f"router never reported {color} healthy before timeout")

    # ------------------------------------------------------------------ #
    # containers                                                          #
    # ------------------------------------------------------------------ #
    def _start_color(self, color: str, version: str) -> str:
        """(Re)start all containers for `color`; return the primary probe URL."""
        cfg = self.cfg
        tag = cfg.image_tag(version)
        if not self.engine.image_exists(tag):
            raise DeploymentError(
                f"image {tag} is missing - build it first (deploy builds automatically)"
            )
        env = {
            "APP_VERSION": version,
            "APP_COLOR": color,
            "APP_PORT": str(cfg.app_container_port),
        }
        for pair in cfg.app_extra_env.split(","):
            if "=" in pair:
                key, value = pair.strip().split("=", 1)
                if key:
                    env[key] = value
        names = cfg.container_names(color)
        for name in names:
            self.engine.remove(name, force=True)
        for name in names:
            self._log_info(f"starting {color} container ({name}) from {tag}")
            self.engine.run_container(
                name=name,
                image=tag,
                network=cfg.network,
                env=env,
                labels={
                    "bgdeploy.managed": "true",
                    "bgdeploy.role": "app",
                    "bgdeploy.color": color,
                    "bgdeploy.version": version,
                },
                publish_bind=cfg.publish_bind,
                container_port=cfg.app_container_port,
            )

        # Wait until the primary instance answers health; replicas are identical
        # images, so a passing primary is a good smoke target.
        primary = names[0]
        deadline = time.monotonic() + cfg.health_wait_timeout
        last_logs = ""
        while time.monotonic() < deadline:
            port = self.engine.host_port(primary, cfg.app_container_port)
            if port:
                url = f"http://127.0.0.1:{port}"
                if http_ok(url + cfg.app_health_path, cfg.smoke_timeout):
                    self._log_info(f"{color} is healthy at {url} ({len(names)} replica(s))")
                    return url
                last_logs = self.engine.logs(primary, tail=30)
            self._sleep(cfg.health_poll)
        detail = last_logs or "no logs captured"
        raise DeploymentError(
            f"{color} containers {names} did not become healthy in time. Logs:\n{detail}"
        )

    def _cleanup_color(self, color: str) -> None:
        for name in self.cfg.container_names(color):
            self.engine.remove(name, force=True)

    # ------------------------------------------------------------------ #
    # smoke                                                               #
    # ------------------------------------------------------------------ #
    def _run_smoke(self, url: str, version: str) -> SmokeReport:
        cases = substitute(load_cases(self.cfg.smoke_file), version)
        self._log_info(f"running {len(cases)} smoke checks against {url}")
        report = run_smoke(url, cases)
        for check in report.checks:
            mark = "PASS" if check.ok else "FAIL"
            self._log_info(f"  smoke {mark} {check.name} ({check.latency_ms:.0f}ms) {check.detail}")
        self._log_info(f"  -> {report.summary()}")
        return report

    # ------------------------------------------------------------------ #
    # deploy                                                              #
    # ------------------------------------------------------------------ #
    def deploy(self, version: str, rebuild: bool = True, skip_smoke: bool = False) -> dict:
        if not version:
            raise DeploymentError("a version string is required (e.g. v1.2.0)")
        version = str(version)
        cfg = self.cfg
        with FileLock(cfg.lock_file):
            state = self.state_store.load()
            active = state.get("active_color")
            target = "blue" if active is None else opposite(active)
            self._log_info(f"deploying {version} to {target} (active: {active})")

            self.ensure_router()

            tag = cfg.image_tag(version)
            if rebuild or not self.engine.image_exists(tag):
                self._log_info(f"building image {tag}")
                self.engine.build(tag, cfg.service_dir, cfg.service_dockerfile)
            else:
                self._log_info(f"reusing existing image {tag}")

            url = self._start_color(target, version)

            # --- gate 1: smoke tests must pass before any traffic moves ---
            report = self._run_smoke(url, version) if not skip_smoke else None
            if report is not None and not report.passed:
                self._cleanup_color(target)
                self.state_store.add_event(
                    state, "deploy", color=target, version=version,
                    result="failed", detail=f"smoke tests failed: {report.summary()}",
                )
                raise DeploymentError(
                    f"smoke tests failed on {target}; release aborted, traffic untouched. "
                    f"{report.summary()}"
                )

            # --- shift traffic gradually, then verify from the client side ---
            self._wait_router_sees_backend(target)
            if active is None:
                # Bootstrap deployment: point the router straight at the new color.
                self.weights.set(target, 100)
                self._sleep(cfg.shift_dwell)
            else:
                for step in cfg.shift_steps:
                    self._log_info(f"shifting traffic to {target}: {step}%")
                    self.weights.set(target, step)
                    self._sleep(cfg.shift_dwell)

            ok, result = self._verify_shift(target)
            self._log_info(
                f"traffic verification: counts={result.get('counts')} errors={result.get('errors')}"
            )
            if not ok:
                if active is not None:
                    self._log_info(f"verification failed - automatically rolling back to {active}")
                    self.weights.set(active, 100)
                    self._sleep(cfg.shift_dwell)
                self._cleanup_color(target)
                self.state_store.add_event(
                    state, "deploy", color=target, version=version,
                    result="failed",
                    detail=f"traffic verification failed; counts={result.get('counts')}",
                )
                raise DeploymentError(
                    f"traffic verification failed for {target}; "
                    f"counts={result.get('counts')}, errors={result.get('errors')}"
                )

            # --- commit ---
            colors = state.setdefault("colors", {})
            colors[target] = {"version": version, "deployed_at": _now_iso()}
            previous = None
            if active is not None:
                previous = {"color": active, "version": state.get("active_version")}
            state["previous"] = previous
            state["active_color"] = target
            state["active_version"] = version
            state.pop("failover", None)  # a fresh commit ends any recovery failover
            self.state_store.add_event(
                state, "deploy", color=target, version=version,
                result="ok", detail=f"traffic shifted to {target}",
            )
            self._log_info(f"deploy {version} committed: active={target}")

            if not cfg.keep_old and active is not None:
                self._log_info(f"tearing down old color {active} (keep_old=false)")
                self._cleanup_color(active)

            return {
                "action": "deploy",
                "result": "ok",
                "version": version,
                "color": target,
                "image": tag,
                "previous": previous,
            }

    # ------------------------------------------------------------------ #
    # rollback                                                            #
    # ------------------------------------------------------------------ #
    def rollback(self, skip_smoke: bool = False) -> dict:
        cfg = self.cfg
        with FileLock(cfg.lock_file):
            state = self.state_store.load()
            active = state.get("active_color")
            previous = state.get("previous")
            if not previous:
                raise DeploymentError("nothing to roll back to - no previous deployment recorded")
            if previous.get("color") == active:
                raise DeploymentError(
                    f"already running {previous.get('version')} on {previous.get('color')}"
                )
            color, version = previous["color"], previous["version"]
            self._log_info(f"rolling back to {version} on {color} (active: {active})")

            self.ensure_router()

            # Bring the previous color up from its recorded version if needed.
            name = cfg.container_name(color)
            running = self.engine.container_running(name)
            label_version = (self.engine.labels(name) or {}).get("bgdeploy.version")
            if not running or label_version != version:
                if not running:
                    self._log_info(f"{color} container is down - starting it again")
                else:
                    self._log_info(f"{color} is running {label_version} - recycling to {version}")
                url = self._start_color(color, version)
            else:
                port = self.engine.host_port(name, cfg.app_container_port)
                if port is None:
                    raise DeploymentError(f"{color} container has no published port")
                url = f"http://127.0.0.1:{port}"

            # Re-verify the rollback target before flipping traffic.
            report = self._run_smoke(url, version) if not skip_smoke else None
            if report is not None and not report.passed:
                self.state_store.add_event(
                    state, "rollback", color=color, version=version,
                    result="failed", detail=f"smoke tests failed: {report.summary()}",
                )
                raise DeploymentError(
                    f"rollback smoke tests failed on {color}; traffic left as-is. "
                    f"{report.summary()}"
                )

            self._wait_router_sees_backend(color)
            self.weights.set(color, 100)
            self._sleep(cfg.shift_dwell)

            ok, result = self._verify_shift(color)
            if not ok:
                if active is not None:
                    self.weights.set(active, 100)
                    self._sleep(cfg.shift_dwell)
                self.state_store.add_event(
                    state, "rollback", color=color, version=version,
                    result="failed",
                    detail=f"traffic verification failed; counts={result.get('counts')}",
                )
                raise DeploymentError(
                    f"rollback verification failed for {color}; traffic restored to {active}"
                )

            state["previous"] = {"color": active, "version": state.get("active_version")}
            state["active_color"] = color
            state["active_version"] = version
            state.pop("failover", None)  # a rollback ends any recovery failover
            self.state_store.add_event(
                state, "rollback", color=color, version=version,
                result="ok", detail=f"traffic shifted back to {color}",
            )
            self._log_info(f"rollback committed: active={color} ({version})")

            if not cfg.keep_old and active is not None:
                self._cleanup_color(active)

            return {
                "action": "rollback",
                "result": "ok",
                "version": version,
                "color": color,
            }

    # ------------------------------------------------------------------ #
    # status                                                              #
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        state = self.state_store.load()
        cfg = self.cfg
        containers = {}
        for color in COLORS:
            instances = []
            for name in cfg.container_names(color):
                labels = self.engine.labels(name)
                instances.append({
                    "name": name,
                    "running": self.engine.container_running(name),
                    "version": labels.get("bgdeploy.version"),
                    "host_port": self.engine.host_port(name, cfg.app_container_port),
                })
            running_any = any(i["running"] for i in instances)
            primary = instances[0] if instances else {}
            containers[color] = {
                "running": running_any,
                "version": primary.get("version"),
                "host_port": primary.get("host_port"),
                "instances": instances,
            }
        return {
            "engine": self.engine.binary,
            "state": state,
            "weights": self.weights.current(),
            "containers": containers,
            "router_url": self._router_url(),
        }
