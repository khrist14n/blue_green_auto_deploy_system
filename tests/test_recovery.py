import pytest

from bgdeploy.config import Config
from bgdeploy.recovery import RecoveryMonitor
from bgdeploy.state import StateStore
from bgdeploy.traffic import TrafficWeightsStore

from conftest import FakeEngine

ACTIVE = "green"
OTHER = "blue"


def _build(tmp_path, *, restart_cooldown=1000.0, auto_restore=True, restore_steady=3):
    cfg = Config().with_overrides(
        data_dir=str(tmp_path),
        restart_cooldown=restart_cooldown,
        auto_restore=auto_restore,
        restore_steady=restore_steady,
    )
    engine = FakeEngine()
    for color, running in ((ACTIVE, True), (OTHER, True)):
        engine.containers[f"app-{color}"] = {
            "image": f"bgdeploy/service:v1",
            "network": "bgdeploy",
            "env": {"APP_COLOR": color},
            "labels": {"bgdeploy.managed": "true", "bgdeploy.color": color,
                       "bgdeploy.version": "v1"},
            "running": running,
            "logs": "started",
        }
    store = StateStore(cfg.state_file)
    state = store.load()
    state["active_color"] = ACTIVE
    state["active_version"] = "v2"
    store.save(state)
    weights = TrafficWeightsStore(cfg.weights_file)
    weights.set(ACTIVE, 100)

    backends = {
        ACTIVE: {"healthy": True},
        OTHER: {"healthy": True},
    }
    clocks = {"now": 0.0}

    def fetch(url):
        return {"backends": backends, "weights": {}}

    monitor = RecoveryMonitor(
        cfg,
        engine=engine,
        state_store=store,
        weights=weights,
        fetch_health=fetch,
        sleep=lambda _s: None,
        clock=lambda: clocks["now"],
        log=lambda *_a: None,
    )
    return cfg, engine, store, weights, monitor, backends, clocks


def _incidents(store) -> list[dict]:
    return store.load()["incidents"]


def test_healthy_active_color_takes_no_action(tmp_path):
    _cfg, _engine, store, weights, monitor, _backends, _clocks = _build(tmp_path)
    actions = monitor.poll_once()
    assert actions == []
    assert weights.current() == {ACTIVE: 100, OTHER: 0}
    assert _incidents(store) == []


def test_failover_when_active_color_unhealthy(tmp_path):
    _cfg, engine, store, weights, monitor, backends, _clocks = _build(tmp_path)
    backends[ACTIVE]["healthy"] = False  # green is down

    actions = monitor.poll_once()

    assert any("failover" in a for a in actions)
    assert weights.current() == {ACTIVE: 0, OTHER: 100}
    kinds = [i["kind"] for i in _incidents(store)]
    assert kinds.count("failover") == 1

    # second poll must not spam another failover incident
    monitor.poll_once()
    failovers = [i for i in _incidents(store) if i["kind"] == "failover"]
    assert len(failovers) == 1


def test_degraded_when_both_colors_unhealthy(tmp_path):
    _cfg, engine, store, weights, monitor, backends, _clocks = _build(tmp_path)
    backends[ACTIVE]["healthy"] = False
    backends[OTHER]["healthy"] = False

    actions = monitor.poll_once()

    assert any("degraded" in a for a in actions)
    # no failover possible - weights stay put
    assert weights.current() == {ACTIVE: 100, OTHER: 0}
    kinds = [i["kind"] for i in _incidents(store)]
    assert "degraded" in kinds


def test_self_heal_restarts_down_container_and_respects_cooldown(tmp_path):
    _cfg, engine, store, weights, monitor, backends, clocks = _build(
        tmp_path, restart_cooldown=10.0
    )
    engine.containers["app-green"]["running"] = False
    backends[ACTIVE]["healthy"] = False

    monitor.poll_once()          # t=0 -> restart allowed
    assert "app-green" in engine.restarted

    monitor.poll_once()          # t=0 again -> inside cooldown, no restart
    assert engine.restarted.count("app-green") == 1

    clocks["now"] = 100.0        # cooldown elapsed
    backends[ACTIVE]["healthy"] = False
    monitor.poll_once()
    assert engine.restarted.count("app-green") == 2
    kinds = [i["kind"] for i in _incidents(store)]
    assert kinds.count("self-heal") == 2


def test_router_unreachable_restarts_crashed_active_container(tmp_path):
    _cfg, engine, store, weights, monitor, _backends, _clocks = _build(tmp_path)

    def broken_fetch(url):
        raise RuntimeError("connection refused")

    monitor._fetch_health = broken_fetch
    engine.containers["app-green"]["running"] = False

    actions = monitor.poll_once()
    assert any("router unreachable" in a for a in actions)
    assert "app-green" in engine.restarted
    kinds = [i["kind"] for i in _incidents(store)]
    assert "monitor" in kinds
    assert "self-heal" in kinds


def test_automatic_restore_after_steady_healthy_checks(tmp_path):
    _cfg, engine, store, weights, monitor, backends, _clocks = _build(tmp_path)
    backends[ACTIVE]["healthy"] = False
    monitor.poll_once()  # failover -> weights to blue

    assert weights.current() == {OTHER: 100, ACTIVE: 0}
    backends[ACTIVE]["healthy"] = True  # green recovers

    monitor.poll_once()  # steady 1
    assert weights.current() == {OTHER: 100, ACTIVE: 0}
    monitor.poll_once()  # steady 2
    assert weights.current() == {OTHER: 100, ACTIVE: 0}
    monitor.poll_once()  # steady 3 -> restore
    assert weights.current() == {ACTIVE: 100, OTHER: 0}
    assert _incidents(store)[0]["kind"] == "restore"


def test_no_auto_restore_when_disabled(tmp_path):
    _cfg, engine, store, weights, monitor, backends, _clocks = _build(
        tmp_path, auto_restore=False
    )
    backends[ACTIVE]["healthy"] = False
    monitor.poll_once()  # failover
    backends[ACTIVE]["healthy"] = True

    for _ in range(5):
        monitor.poll_once()

    # failover stays sticky until a human acts
    assert weights.current() == {OTHER: 100, ACTIVE: 0}
    kinds = [i["kind"] for i in _incidents(store)]
    assert "restore" not in kinds


def test_alert_posted_on_failover_and_rate_limited(tmp_path):
    _cfg, engine, store, weights, monitor, backends, clocks = _build(
        tmp_path, restart_cooldown=1000.0
    )
    monitor.cfg = monitor.cfg.with_overrides(alert_webhook="http://alerts.example/hook")
    posted = []
    monitor._post_alert = lambda url, payload: posted.append((url, payload))

    backends[ACTIVE]["healthy"] = False  # green down, blue healthy
    monitor.poll_once()
    assert len(posted) == 1
    url, payload = posted[0]
    assert url == "http://alerts.example/hook"
    assert payload["kind"] == "failover"
    assert payload["active_color"] == ACTIVE
    assert payload["weights"] == {ACTIVE: 0, OTHER: 100}

    # inside the min interval: no second alert even though we degrade further
    backends[OTHER]["healthy"] = False
    monitor.poll_once()
    assert len(posted) == 1

    # after the interval elapses a new event may alert again
    clocks["now"] = 1000.0
    monitor._degraded = False  # simulate a fresh degraded episode
    backends[OTHER]["healthy"] = False
    backends[ACTIVE]["healthy"] = False
    monitor.poll_once()
    assert len(posted) == 2
    assert posted[1][1]["kind"] == "degraded"


def test_no_alert_without_webhook_configured(tmp_path):
    _cfg, engine, store, weights, monitor, backends, _clocks = _build(tmp_path)
    monitor._post_alert = lambda url, payload: (_ for _ in ()).throw(AssertionError("must not post"))
    backends[ACTIVE]["healthy"] = False
    actions = monitor.poll_once()
    assert any("failover" in a for a in actions)  # recovery still happened


def test_no_active_color_reports_idle(tmp_path):
    _cfg, engine, store, weights, monitor, _backends, _clocks = _build(tmp_path)
    state = store.load()
    state["active_color"] = None
    store.save(state)

    actions = monitor.poll_once()
    assert any("no active color" in a for a in actions)
    assert weights.current() == {ACTIVE: 100, OTHER: 0}
