"""Real-engine integration tests (docker and/or podman).

These exercise the ContainerEngine abstraction against whatever container
engine is actually installed and reachable on the host. Every test is
parametrized over the available engines and is skipped automatically when no
engine is present (so the default suite stays runnable on bare machines).

Disable entirely with:  BG_TEST_REAL_ENGINE=0
"""
import os
import time
import urllib.request

import pytest

from bgdeploy.engine import select_engine

IMAGE = "python:3.12-slim"


def _available_engines() -> list:
    if os.environ.get("BG_TEST_REAL_ENGINE", "1") == "0":
        return []
    engines = []
    for candidate in ("docker", "podman"):
        try:
            engines.append(select_engine(candidate))
        except Exception:  # noqa: BLE001 - engine missing or unreachable
            continue
    return engines


# [None] placeholder keeps parametrization valid when no engine exists; the
# fixture skips each test instead of failing collection.
_REAL_ENGINES = _available_engines()


@pytest.fixture(params=_REAL_ENGINES or [None],
                ids=[e.binary for e in _REAL_ENGINES] or ["no-engine"])
def engine(request):
    if request.param is None:
        pytest.skip("no docker or podman engine available "
                    "(install one or set BG_TEST_REAL_ENGINE=0)")
    return request.param


def _unique(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{int(time.time() * 1000)}"


def _wait_running(engine, name, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.container_running(name):
            return
        time.sleep(0.5)
    raise AssertionError(f"container {name} did not start within {timeout}s")


def _http_status(url: str, timeout: float = 15.0) -> int:
    """GET until the server inside the container is actually accepting
    connections (inspect reports Running before the app binds its port)."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status
        except OSError as exc:  # connection refused/reset while still warming up
            last_error = exc
            time.sleep(0.5)
    raise AssertionError(f"{url} did not answer within {timeout}s: {last_error}")


def test_engine_lifecycle_run_serve_restart_stop(engine):
    """Full container lifecycle: network -> run -> publish -> serve -> restart
    -> stop -> remove, verified through the published host port."""
    net = _unique("bgtest-net")
    name = _unique("bgtest-app")
    try:
        # network_ensure is idempotent
        engine.network_ensure(net)
        engine.network_ensure(net)

        engine.run_container(
            name=name,
            image=IMAGE,
            network=net,
            env={"BG_TEST_MARKER": "1"},
            labels={"bgdeploy.managed": "true", "bgdeploy.test": "real"},
            publish_bind="127.0.0.1",
            container_port=8000,  # random ephemeral host port
            command=["python3", "-m", "http.server", "8000"],
        )
        _wait_running(engine, name)

        # the published host port is discoverable and actually serves
        port = engine.host_port(name, 8000)
        assert port, "published host port should be discoverable via inspect"
        assert _http_status(f"http://127.0.0.1:{port}/") == 200

        # container addressing on the network + labels round-trip
        assert engine.ip(name, net), "container should have an IP on the network"
        labels = engine.labels(name)
        assert labels.get("bgdeploy.managed") == "true"
        assert labels.get("bgdeploy.test") == "real"

        # restart: re-read the published port (daemons may rebind random ports
        # on restart) and verify the container keeps serving
        engine.restart(name)
        _wait_running(engine, name)
        port = engine.host_port(name, 8000)
        assert port, "host port should be discoverable after restart"
        assert _http_status(f"http://127.0.0.1:{port}/") == 200

        # stop -> not running; remove -> gone
        engine.stop(name)
        assert not engine.container_running(name)
        engine.remove(name, force=True)
        assert not engine.container_exists(name)
    finally:
        engine.remove(name, force=True)
        engine.network_remove(net)


def test_build_image_and_image_exists(engine, tmp_path):
    """Build parity: `docker build` and `podman build` (buildah) both work."""
    tag = f"bgtest-build:{os.getpid()}-{int(time.time())}"
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nRUN true\nCMD [\"true\"]\n"
    )
    try:
        engine.build(tag, str(tmp_path))
        assert engine.image_exists(tag)
    finally:
        engine.run(["image", "rm", tag], check=False)


def test_ps_managed_and_logs(engine):
    """Managed-container listing and log capture work on both engines."""
    net = _unique("bgtest-net")
    name = _unique("bgtest-log")
    try:
        engine.network_ensure(net)
        engine.run_container(
            name=name,
            image=IMAGE,
            network=net,
            labels={"bgdeploy.managed": "true"},
            command=["python3", "-c", "import time; time.sleep(300)"],
        )
        _wait_running(engine, name)
        assert name in engine.ps_managed()
        # logs work even on a still-running container
        assert isinstance(engine.logs(name, tail=5), str)
    finally:
        engine.remove(name, force=True)
        engine.network_remove(net)