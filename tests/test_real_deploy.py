"""Full blue/green pipeline against a real container engine (opt-in).

Run with:  pytest -m realdeploy

Builds the real service + router images and runs `deploy v1 -> deploy v2 ->
rollback` through the actual router, verifying served versions and colors at
each step. Exercised against docker and/or podman (pin one with BG_ENGINE).
Every piece of state (data dir, container names, network, router port) is
made unique per run so it is safe to execute alongside a live stack.
"""
import json
import os
import socket
import time
import urllib.request

import pytest

from bgdeploy.config import Config
from bgdeploy.deployer import BlueGreenDeployer
from bgdeploy.engine import select_engine
from bgdeploy.state import StateStore
from bgdeploy.traffic import TrafficWeightsStore, fetch_router_health

pytestmark = pytest.mark.realdeploy


def _real_engines() -> list:
    pinned = os.environ.get("BG_ENGINE")
    candidates = [pinned] if pinned in ("docker", "podman") else ["docker", "podman"]
    engines = []
    for candidate in candidates:
        try:
            engines.append(select_engine(candidate))
        except Exception:  # noqa: BLE001 - engine missing or unreachable
            continue
    return engines


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_REAL_ENGINES = _real_engines()


@pytest.fixture(params=_REAL_ENGINES or [None],
                ids=[e.binary for e in _REAL_ENGINES] or ["no-engine"])
def engine(request):
    if request.param is None:
        pytest.skip("no docker or podman engine available")
    return request.param


def _stack_cfg(tmp_path, engine_binary: str) -> Config:
    suffix = f"{engine_binary}-{os.getpid()}-{int(time.time())}"
    return Config.from_env().with_overrides(
        data_dir=str(tmp_path),
        container_prefix=f"bgtest-{suffix[:10]}",
        router_name=f"bgtest-router-{suffix[:16]}",
        router_host_port=_free_port(),
        network=f"bgtest-net-{suffix[:16]}",
        shift_steps=(100,),   # one-shot shift keeps the real run fast
        shift_dwell=2.0,      # > the router's 1s weights poll interval
        verify_samples=4,
        health_wait_timeout=120.0,
    )


def _http_served(url: str) -> tuple[str, str]:
    """(version, color) as served by the router for /version."""
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.loads(resp.read().decode())
        color = resp.headers.get("X-Served-By", "?")
    return str(body.get("version", "?")), color


def test_full_deploy_cycle(engine, tmp_path):
    cfg = _stack_cfg(tmp_path, engine.binary)
    deployer = BlueGreenDeployer(
        cfg,
        engine=engine,
        state_store=StateStore(cfg.state_file),
        weights=TrafficWeightsStore(cfg.weights_file),
        router_checker=fetch_router_health,
        log=lambda *a: None,
    )
    url = f"http://127.0.0.1:{cfg.router_host_port}/version"

    try:
        # bootstrap: v1 lands on blue and the router serves it
        summary = deployer.deploy(version="v1")
        assert summary["color"] == "blue" and summary["version"] == "v1"
        version, color = _http_served(url)
        assert version == "v1" and color == "blue"

        # v2 shifts to green
        summary = deployer.deploy(version="v2")
        assert summary["color"] == "green" and summary["version"] == "v2"
        version, color = _http_served(url)
        assert version == "v2" and color == "green"

        # rollback returns to blue v1
        summary = deployer.rollback()
        assert summary["color"] == "blue" and summary["version"] == "v1"
        version, color = _http_served(url)
        assert version == "v1" and color == "blue"
    finally:
        # remove only this stack's containers + network
        for color in ("blue", "green"):
            for name in cfg.container_names(color):
                engine.remove(name, force=True)
        engine.remove(cfg.router_name, force=True)
        engine.network_remove(cfg.network)