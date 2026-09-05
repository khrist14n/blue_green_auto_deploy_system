"""Shared fixtures.

The unit tests never talk to a container daemon or to the real router image.
Instead:

* FakeEngine records every call an engine would make and lets tests control
  host ports / container state, so the orchestration logic is fully exercised;
* BackendServer is a real threaded HTTP server that behaves like one color of
  the demo service (health, version, fault injection), so smoke/health code is
  tested over real sockets.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Make the repo root importable so `import router.app` works from any runner.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bgdeploy.traffic import TrafficWeightsStore  # noqa: E402

# --------------------------------------------------------------------------- #
# HTTP backends                                                               #
# --------------------------------------------------------------------------- #
class _JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test noise
        pass

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        srv = self.server
        if self.path == "/__router/ping":
            return self._reply(200, {"status": "ok"})
        if self.path in srv.fail_paths or srv.fail:
            return self._reply(503, {"status": "unhealthy"})
        if self.path == "/healthz":
            return self._reply(200, {"status": "ok"})
        if self.path == "/readyz":
            return self._reply(200, {"status": "ready"})
        if self.path == "/version":
            return self._reply(200, {"version": srv.version, "color": srv.color, "status": "ok"})
        if self.path == "/":
            return self._reply(200, {
                "service": "bgdeploy-demo",
                "version": srv.version,
                "color": srv.color,
                "healthy": True,
            })
        return self._reply(404, {"error": "not found"})


class BackendServer:
    """Threaded HTTP server behaving like one color of the demo service."""

    def __init__(self, color: str, version: str = "v1", fail: bool = False,
                 fail_paths: set[str] | None = None):
        self.color = color
        self.version = version
        self.fail = fail
        self.fail_paths = set(fail_paths or set())
        handler = type("H", (_JsonHandler,), {})
        handler.server = None
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.color = color
        self._httpd.version = version
        self._httpd.fail = fail
        self._httpd.fail_paths = self.fail_paths
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_fail(self, fail: bool) -> None:
        self._httpd.fail = fail

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def backend_factory():
    servers = []

    def make(color: str, version: str = "v1", fail: bool = False,
             fail_paths: set[str] | None = None) -> BackendServer:
        srv = BackendServer(color, version, fail, fail_paths)
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.stop()


# --------------------------------------------------------------------------- #
# Fake container engine                                                       #
# --------------------------------------------------------------------------- #
class FakeEngine:
    """Records every operation a real engine would perform."""

    def __init__(self, ports: dict | None = None, binary: str = "docker"):
        self.binary = binary
        self._ports = dict(ports or {})
        self._next_port = 21000
        self.containers: dict[str, dict] = {}
        self.networks: set[str] = set()
        self.images: set[str] = set()
        self.calls: list[dict] = []
        self.restarted: list[str] = []
        self.removed: list[str] = []
        self.builds: list[tuple[str, str]] = []

    # engine API ---------------------------------------------------------
    def available(self) -> bool:
        return True

    def network_ensure(self, name: str) -> None:
        self.networks.add(name)

    def image_exists(self, tag: str) -> bool:
        return tag in self.images

    def build(self, tag: str, context: str, dockerfile: str = "Dockerfile") -> None:
        self.builds.append((tag, context))
        self.images.add(tag)

    def run_container(self, name, image, network, env=None, labels=None,
                      publish_bind=None, publish_host_port=None, container_port=None,
                      extra_args=None) -> str:
        self.containers[name] = {
            "image": image,
            "network": network,
            "env": dict(env or {}),
            "labels": dict(labels or {}),
            "running": True,
            "logs": "fake container started",
        }
        self.calls.append({
            "op": "run", "name": name, "image": image,
            "labels": dict(labels or {}),
        })
        if name not in self._ports:
            if publish_host_port:
                self._ports[name] = publish_host_port
            else:
                self._ports[name] = self._next_port
                self._next_port += 1
        return name

    def remove(self, name: str, force: bool = True) -> None:
        self.removed.append(name)
        self.containers.pop(name, None)

    def container_exists(self, name: str) -> bool:
        return name in self.containers

    def container_running(self, name: str) -> bool:
        info = self.containers.get(name)
        return bool(info and info["running"])

    def labels(self, name: str) -> dict:
        info = self.containers.get(name)
        return dict(info["labels"]) if info else {}

    def host_port(self, name: str, container_port: int) -> int | None:
        if name not in self.containers:
            return None
        return self._ports.get(name)

    def ip(self, name: str, network: str) -> str | None:
        return "10.0.0.1" if name in self.containers else None

    def restart(self, name: str) -> None:
        if name not in self.containers:
            raise RuntimeError(f"no such container: {name}")
        self.restarted.append(name)
        self.containers[name]["running"] = True
        self.containers[name]["logs"] += "\nrestarted"

    def logs(self, name: str, tail: int = 50) -> str:
        info = self.containers.get(name)
        return info["logs"] if info else ""


# --------------------------------------------------------------------------- #
# Weights wrapper that records every write                                    #
# --------------------------------------------------------------------------- #
class RecordingWeights(TrafficWeightsStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sets: list[tuple[str, int]] = []

    def set(self, color: str, percent: int) -> dict:
        self.sets.append((color, percent))
        return super().set(color, percent)
