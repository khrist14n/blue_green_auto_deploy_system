"""Auto-recovery.

Two layers cooperate to keep user traffic flowing:

* the *router* fails over per request - if the weighted favorite backend is
  unhealthy, the request is served by the other color (instant, zero config);
* this *RecoveryMonitor* makes recovery durable - it watches the router's view
  of backend health and, when the active color stays unhealthy:

    1. FAILOVER  - shifts 100% of traffic to the healthy color (if any),
    2. SELF-HEAL - restarts the sick container (rate limited), so it can come
       back healthy and later be restored, and
    3. RESTORE   - once the original color has been healthy for N consecutive
       checks (and auto_restore is on), traffic shifts back to it.

Every decision is recorded as an incident in state.json. Run with
`bgdeploy watch` for continuous monitoring or `bgdeploy recover` for a single
pass (e.g. from cron / systemd timer).
"""
from __future__ import annotations

import json
import time
import urllib.request

from .config import Config
from .engine import ContainerEngine
from .state import StateStore
from .traffic import TrafficWeightsStore, fetch_router_health


class RecoveryMonitor:
    def __init__(
        self,
        cfg: Config,
        engine: ContainerEngine,
        state_store: StateStore,
        weights: TrafficWeightsStore,
        fetch_health=None,   # fetch_health(router_url) -> /__router/health dict
        post_alert=None,     # post_alert(url, payload_dict) -> None (alert webhook)
        sleep=time.sleep,
        clock=time.monotonic,
        log=print,
    ):
        self.cfg = cfg
        self.engine = engine
        self.state_store = state_store
        self.weights = weights
        self._fetch_health = fetch_health or fetch_router_health
        self._post_alert = post_alert or self._default_post_alert
        self._sleep = sleep
        self._clock = clock
        self._log = log
        self._router_url = cfg.router_admin_url()
        self._last_alert: float | None = None
        # monitor-local state (failover marker itself is persisted in state.json
        # so recovery decisions survive monitor restarts / separate CLI runs)
        self._steady_checks = 0
        self._unhealthy_polls: dict = {}
        self._last_restart: dict = {}
        self._degraded = False

    # ------------------------------------------------------------------ #
    def poll_once(self) -> list[str]:
        """Run one recovery pass. Returns human-readable actions taken."""
        cfg = self.cfg
        state = self.state_store.load()
        active = state.get("active_color")
        actions: list[str] = []
        if not active:
            return ["no active color - nothing to recover"]
        # Are we currently in a durable failover? Persisted so a fresh monitor
        # (e.g. `recover` then `watch` as separate processes) keeps the context.
        failed_over = bool(state.get("failover"))

        other = "green" if active == "blue" else "blue"
        try:
            health = self._fetch_health(self._router_url)
        except Exception as exc:  # noqa: BLE001 - router itself may be down
            msg = f"router unreachable from monitor: {exc}"
            actions.append(msg)
            self.state_store.add_incident(state, "monitor", msg)
            self._alert(state, "monitor", msg)
            # Last line of defence: make sure the active container is at least running.
            self._ensure_container_running(active, state, actions)
            return actions

        backends = health.get("backends", {})
        active_ok = bool(backends.get(active, {}).get("healthy"))
        other_ok = bool(backends.get(other, {}).get("healthy"))

        if active_ok or other_ok:
            self._degraded = False
        if active_ok:
            # Active color is healthy again.
            self._unhealthy_polls[active] = 0
            if not failed_over:
                return actions  # all good
            if not cfg.auto_restore:
                return actions  # failover is sticky until a human acts
            self._steady_checks += 1
            if self._steady_checks >= cfg.restore_steady:
                self.weights.set(active, 100)
                state.pop("failover", None)
                self.state_store.add_incident(
                    state, "restore",
                    f"{active} healthy again ({self._steady_checks} checks) - traffic restored",
                )
                self._alert(state, "restore",
                            f"{active} healthy again ({self._steady_checks} checks) - traffic restored")
                actions.append(f"[restore] {active} healthy again; traffic back to {active}")
                self._log(f"[recovery] {active} recovered; traffic restored to {active}")
                self._steady_checks = 0
            return actions

        # ---- the active color is unhealthy: durable failover ----
        self._unhealthy_polls[active] = self._unhealthy_polls.get(active, 0) + 1
        if other_ok:
            self.weights.set(other, 100)
            if not failed_over:
                state["failover"] = {"from": active, "to": other, "since": self._clock()}
                self.state_store.add_incident(
                    state, "failover",
                    f"{active} unhealthy -> all traffic shifted to {other}",
                )
                self._alert(state, "failover", f"{active} unhealthy -> all traffic shifted to {other}")
                actions.append(f"[failover] {active} unhealthy; traffic -> {other}")
                self._log(f"[recovery] {active} unhealthy; shifted traffic to {other}")
        else:
            if not self._degraded:
                self._degraded = True
                self.state_store.add_incident(
                    state, "degraded", f"both {active} and {other} unhealthy",
                )
                self._alert(state, "degraded", f"both {active} and {other} unhealthy")
                actions.append("[degraded] both colors unhealthy")
                self._log(f"[recovery] both {active} and {other} unhealthy")

        # Self-heal the sick containers (rate limited by restart_cooldown).
        for name in cfg.container_names(active):
            self._maybe_restart(name, active, state, actions)
        return actions

    def _ensure_container_running(self, color: str, state: dict, actions: list[str]) -> None:
        """Restart every instance of `color` that is not running (crash recovery)."""
        for name in self.cfg.container_names(color):
            if not self.engine.container_exists(name):
                continue  # never deployed; nothing to restart
            if self.engine.container_running(name):
                continue
            self._maybe_restart(name, color, state, actions)

    def _maybe_restart(self, name: str, color: str | None, state: dict, actions: list[str]) -> None:
        """Restart one container, rate limited per color."""
        cfg = self.cfg
        if not self.engine.container_exists(name):
            return
        now = self._clock()
        key = color or name
        last = self._last_restart.get(key)
        if last is not None and now - last < cfg.restart_cooldown:
            return
        self._log(f"[recovery] restarting {name}")
        try:
            self.engine.restart(name)
            self._last_restart[key] = now
            self.state_store.add_incident(state, "self-heal", f"restarted {name}")
            actions.append(f"[self-heal] restarted {name}")
        except Exception as exc:  # noqa: BLE001
            self.state_store.add_incident(
                state, "self-heal-failed", f"restart of {name} failed: {exc}"
            )
            self._alert(state, "self-heal-failed", f"restart of {name} failed: {exc}")
            actions.append(f"[self-heal-failed] {exc}")

    # ----------------------------------------------------------- alerts ----
    @staticmethod
    def _default_post_alert(url: str, payload: dict) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "bgdeploy-recovery/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"alert webhook answered {resp.status}")

    def _alert(self, state: dict, kind: str, detail: str) -> None:
        """POST an alert to BG_ALERT_WEBHOOK_URL, rate limited."""
        url = self.cfg.alert_webhook
        if not url:
            return
        now = self._clock()
        if self._last_alert is not None and now - self._last_alert < self.cfg.alert_min_interval:
            return
        self._last_alert = now
        payload = {
            "event": "bgdeploy-recovery",
            "kind": kind,
            "detail": detail,
            "active_color": state.get("active_color"),
            "active_version": state.get("active_version"),
            "weights": self.weights.current(),
        }
        try:
            self._post_alert(url, payload)
        except Exception as exc:  # noqa: BLE001 - alerting must never break recovery
            self._log(f"[recovery] alert delivery failed: {exc}")

    # ------------------------------------------------------------------ #
    def run_forever(self, interval: float | None = None) -> None:
        """Continuous monitoring loop - Ctrl+C to stop."""
        interval = interval or self.cfg.check_interval
        self._log(
            f"[recovery] watching active color every {interval}s (router: {self._router_url})"
        )
        try:
            while True:
                try:
                    for action in self.poll_once():
                        self._log(f"[recovery] {action}")
                except Exception as exc:  # noqa: BLE001 - never die
                    self._log(f"[recovery] poll failed: {exc}")
                self._sleep(interval)
        except KeyboardInterrupt:
            self._log("[recovery] stopped")
