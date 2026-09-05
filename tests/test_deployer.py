import json

import pytest

from bgdeploy.config import Config
from bgdeploy.deployer import BlueGreenDeployer, DeploymentError
from bgdeploy.state import StateStore

from conftest import BackendServer, FakeEngine, RecordingWeights


def _write_smoke_file(tmp_path) -> str:
    path = tmp_path / "smoke.json"
    path.write_text(json.dumps([
        {"name": "healthz", "method": "GET", "path": "/healthz",
         "expected_status": 200, "contains": ["ok"]},
        {"name": "version", "method": "GET", "path": "/version",
         "expected_status": 200, "contains": ["{version}", "ok"]},
        {"name": "root", "method": "GET", "path": "/",
         "expected_status": 200, "contains": ["service"]},
    ]))
    return str(path)


class Stack:
    """A fully wired, hermetic deployment stack for tests."""

    def __init__(self, tmp_path, backend_factory, blue_version="v1", green_version="v2",
                 blue_fail=False, green_fail_paths=None):
        self.blue = backend_factory("blue", version=blue_version, fail=blue_fail)
        self.green = backend_factory("green", version=green_version, fail_paths=green_fail_paths)
        self.router_ping = backend_factory("router", version="-")
        self.served = ["blue"]  # what the (stubbed) router currently serves
        self.cfg = Config().with_overrides(
            data_dir=str(tmp_path),
            smoke_file=_write_smoke_file(tmp_path),
            router_host_port=self.router_ping.port,
            router_bind="127.0.0.1",
            shift_dwell=0.0,
            health_poll=0.0,
            health_wait_timeout=20.0,
            verify_samples=5,
        )
        self.engine = FakeEngine(ports={
            "app-blue": self.blue.port,
            "app-green": self.green.port,
        })
        self.weights = RecordingWeights(self.cfg.weights_file)

    def deployer(self) -> BlueGreenDeployer:
        return BlueGreenDeployer(
            self.cfg,
            engine=self.engine,
            state_store=StateStore(self.cfg.state_file),
            weights=self.weights,
            sampler=lambda url, samples: {"counts": {self.served[0]: samples}, "errors": 0},
            sleep=lambda _s: None,
            log=lambda *_a: None,
        )

    def state(self) -> dict:
        return StateStore(self.cfg.state_file).load()


@pytest.fixture
def stack(tmp_path, backend_factory):
    return Stack(tmp_path, backend_factory)


# --------------------------------------------------------------------------- #
def test_bootstrap_deploy_builds_and_activates_blue(stack):
    deployer = stack.deployer()
    summary = deployer.deploy("v1")

    assert summary["action"] == "deploy"
    assert summary["color"] == "blue"
    assert summary["version"] == "v1"

    # image was built from the service context and the router image too
    assert ("bgdeploy/service:v1", "service") in stack.engine.builds
    assert stack.engine.image_exists("bgdeploy/router")

    # container is up with the right labels/env
    info = stack.engine.containers["app-blue"]
    assert info["running"] is True
    assert info["labels"]["bgdeploy.color"] == "blue"
    assert info["labels"]["bgdeploy.version"] == "v1"
    assert info["env"]["APP_VERSION"] == "v1"
    assert info["env"]["APP_COLOR"] == "blue"

    # state committed
    state = stack.state()
    assert state["active_color"] == "blue"
    assert state["active_version"] == "v1"
    assert state["previous"] is None
    assert state["colors"]["blue"]["version"] == "v1"
    assert state["history"][0]["action"] == "deploy"
    assert state["history"][0]["result"] == "ok"

    # router pointed at blue and verified from the client side
    assert stack.weights.current() == {"blue": 100, "green": 0}


def test_second_deploy_targets_green_and_ramps_traffic(stack):
    deployer = stack.deployer()
    deployer.deploy("v1")
    stack.served[0] = "green"  # router now honors the shift

    summary = deployer.deploy("v2")

    assert summary["color"] == "green"
    state = stack.state()
    assert state["active_color"] == "green"
    assert state["active_version"] == "v2"
    assert state["previous"] == {"color": "blue", "version": "v1"}
    assert state["colors"]["green"]["version"] == "v2"
    # blue container kept warm for rollback
    assert stack.engine.container_running("app-blue")

    # traffic was shifted gradually: 25 -> 50 -> 75 -> 100
    assert stack.weights.sets[-4:] == [
        ("green", 25), ("green", 50), ("green", 75), ("green", 100),
    ]
    assert stack.weights.current() == {"blue": 0, "green": 100}


def test_rollback_returns_to_previous_stable(stack):
    deployer = stack.deployer()
    deployer.deploy("v1")
    stack.served[0] = "green"
    deployer.deploy("v2")

    stack.served[0] = "blue"  # router honors the rollback shift
    summary = deployer.rollback()

    assert summary["action"] == "rollback"
    assert summary["color"] == "blue"
    assert summary["version"] == "v1"

    state = stack.state()
    assert state["active_color"] == "blue"
    assert state["active_version"] == "v1"
    assert state["previous"] == {"color": "green", "version": "v2"}
    assert stack.weights.current() == {"blue": 100, "green": 0}
    assert state["history"][0]["action"] == "rollback"


def test_rollback_without_history_is_rejected(stack):
    deployer = stack.deployer()
    with pytest.raises(DeploymentError, match="nothing to roll back"):
        deployer.rollback()


def test_smoke_failure_aborts_before_traffic_moves(tmp_path, backend_factory):
    # green's /version endpoint is broken: smoke must catch it before shifting.
    stack = Stack(tmp_path, backend_factory, green_fail_paths={"/version"})
    deployer = stack.deployer()
    deployer.deploy("v1")
    stack.served[0] = "green"

    with pytest.raises(DeploymentError, match="smoke tests failed"):
        deployer.deploy("v2")

    state = stack.state()
    # nothing changed: still on blue v1, traffic untouched, candidate removed
    assert state["active_color"] == "blue"
    assert state["active_version"] == "v1"
    assert "green" not in state["colors"]
    assert stack.weights.current() == {"blue": 100, "green": 0}
    assert "app-green" in stack.engine.removed
    assert stack.state()["history"][0]["result"] == "failed"


def test_shift_verification_failure_auto_rolls_back(tmp_path, backend_factory):
    stack = Stack(tmp_path, backend_factory)
    deployer = stack.deployer()
    deployer.deploy("v1")
    # router "stuck" serving blue even after the shift to green
    stack.served[0] = "blue"

    with pytest.raises(DeploymentError, match="traffic verification failed"):
        deployer.deploy("v2")

    state = stack.state()
    assert state["active_color"] == "blue"
    assert state["active_version"] == "v1"
    # traffic was automatically pointed back at the previously active color
    assert stack.weights.sets[-1] == ("blue", 100)
    assert stack.weights.current() == {"blue": 100, "green": 0}
    assert "app-green" in stack.engine.removed
    assert stack.state()["history"][0]["result"] == "failed"


def test_deploy_with_replicas_per_color(tmp_path, backend_factory):
    blue = backend_factory("blue", version="v1")
    green = backend_factory("green", version="v2")
    router_ping = backend_factory("router", version="-")
    cfg = Config().with_overrides(
        data_dir=str(tmp_path),
        replicas=2,
        smoke_file=_write_smoke_file(tmp_path),
        router_host_port=router_ping.port,
        shift_dwell=0.0,
        health_poll=0.0,
        health_wait_timeout=20.0,
        verify_samples=5,
    )
    engine = FakeEngine(ports={
        "app-blue-1": blue.port, "app-blue-2": blue.port,
        "app-green-1": green.port, "app-green-2": green.port,
    })
    served = ["blue"]
    weights = RecordingWeights(cfg.weights_file)

    def deployer():
        return BlueGreenDeployer(
            cfg,
            engine=engine,
            state_store=StateStore(cfg.state_file),
            weights=weights,
            sampler=lambda url, samples: {"counts": {served[0]: samples}, "errors": 0},
            sleep=lambda _s: None,
            log=lambda *_a: None,
        )

    summary = deployer().deploy("v1")
    assert summary["color"] == "blue"
    for name in ("app-blue-1", "app-blue-2"):
        info = engine.containers[name]
        assert info["running"] is True
        assert info["labels"]["bgdeploy.version"] == "v1"
        assert info["labels"]["bgdeploy.color"] == "blue"

    # router is configured with a replica list and knows about them
    router = engine.containers["bgdeploy-router"]
    assert router["labels"]["bgdeploy.replicas"] == "2"
    assert "app-blue-1" in router["env"]["BACKEND_BLUE"]
    assert "app-blue-2" in router["env"]["BACKEND_BLUE"]

    # deploy v2 -> green keeps both blue replicas warm
    served[0] = "green"
    summary = deployer().deploy("v2")
    assert summary["color"] == "green"
    assert engine.container_running("app-blue-1")
    assert engine.container_running("app-blue-2")
    assert engine.container_running("app-green-1")
    assert engine.container_running("app-green-2")

    # rollback reuses the warm blue replicas and flips traffic
    served[0] = "blue"
    summary = deployer().rollback()
    assert summary["color"] == "blue"
    assert StateStore(cfg.state_file).load()["active_color"] == "blue"
    assert engine.container_running("app-green-1")  # green still warm for the next deploy

    # snapshot lists instances per color
    snap = deployer().snapshot()
    assert len(snap["containers"]["blue"]["instances"]) == 2
    assert snap["containers"]["blue"]["running"] is True


def test_cleanup_removes_all_replicas(tmp_path, backend_factory):
    blue = backend_factory("blue", version="v1")
    router_ping = backend_factory("router", version="-")
    cfg = Config().with_overrides(
        data_dir=str(tmp_path),
        replicas=2,
        smoke_file=_write_smoke_file(tmp_path),
        router_host_port=router_ping.port,
        shift_dwell=0.0,
        health_poll=0.0,
        health_wait_timeout=20.0,
        verify_samples=5,
    )
    engine = FakeEngine(ports={"app-blue-1": blue.port, "app-blue-2": blue.port})
    weights = RecordingWeights(cfg.weights_file)
    deployer = BlueGreenDeployer(
        cfg, engine=engine, state_store=StateStore(cfg.state_file), weights=weights,
        sampler=lambda url, samples: {"counts": {"blue": samples}, "errors": 0},
        sleep=lambda _s: None, log=lambda *_a: None,
    )

    # a smoke failure must tear down every replica of the candidate color
    blue.fail_paths.update({"/", "/version"})  # healthy to start, fails its smoke suite
    try:
        deployer.deploy("v1")
    except DeploymentError:
        pass
    else:
        raise AssertionError("expected deploy to fail against broken candidate")
    # both replicas were started and then cleaned up on failure
    assert "app-blue-1" in engine.removed
    assert "app-blue-2" in engine.removed


def test_app_extra_env_is_injected_into_container(tmp_path, backend_factory):
    stack = Stack(tmp_path, backend_factory)
    cfg = stack.cfg.with_overrides(app_extra_env="UNHEALTHY_AFTER=30,CUSTOM=yes")
    deployer = BlueGreenDeployer(
        cfg,
        engine=stack.engine,
        state_store=StateStore(cfg.state_file),
        weights=RecordingWeights(cfg.weights_file),
        sampler=lambda url, samples: {"counts": {"blue": samples}, "errors": 0},
        sleep=lambda _s: None,
        log=lambda *_a: None,
    )
    deployer.deploy("v1")
    env = stack.engine.containers["app-blue"]["env"]
    assert env["UNHEALTHY_AFTER"] == "30"
    assert env["CUSTOM"] == "yes"
    assert env["APP_VERSION"] == "v1"


def test_snapshot_reports_live_state(stack):
    deployer = stack.deployer()
    deployer.deploy("v1")
    snap = deployer.snapshot()
    assert snap["engine"] == "docker"
    assert snap["state"]["active_color"] == "blue"
    assert snap["weights"] == {"blue": 100, "green": 0}
    assert snap["containers"]["blue"]["running"] is True
    assert snap["containers"]["blue"]["version"] == "v1"
    assert snap["containers"]["blue"]["host_port"] == stack.blue.port
    assert snap["containers"]["green"]["running"] is False
