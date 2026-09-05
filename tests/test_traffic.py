import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bgdeploy.traffic import TrafficWeightsStore, sample_served, verify_full_shift

from router.app import pick_backend


# --------------------------------------------------------------------------- #
# weights store                                                               #
# --------------------------------------------------------------------------- #
def test_weights_default_when_file_missing(tmp_path):
    store = TrafficWeightsStore(str(tmp_path / "weights.json"))
    assert store.current() == {"blue": 100, "green": 0}


def test_weights_set_and_persist(tmp_path):
    store = TrafficWeightsStore(str(tmp_path / "weights.json"))
    store.set("green", 25)
    assert store.current() == {"blue": 75, "green": 25}

    reloaded = TrafficWeightsStore(str(tmp_path / "weights.json")).load()
    assert reloaded == {"blue": 75, "green": 25}


def test_weights_complement_and_clamping(tmp_path):
    store = TrafficWeightsStore(str(tmp_path / "weights.json"))
    store.set("blue", 200)
    assert store.current() == {"blue": 100, "green": 0}
    store.set("green", 0)
    assert store.current() == {"blue": 100, "green": 0}


def test_weights_fallback_on_bad_file(tmp_path):
    path = tmp_path / "weights.json"
    path.write_text("garbage")
    store = TrafficWeightsStore(str(path))
    assert store.current() == {"blue": 100, "green": 0}


def test_weights_set_rejects_unknown_color(tmp_path):
    store = TrafficWeightsStore(str(tmp_path / "weights.json"))
    try:
        store.set("purple", 10)
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown color")


# --------------------------------------------------------------------------- #
# weighted pick (pure function from the router)                               #
# --------------------------------------------------------------------------- #
def test_pick_backend_weighted_blue():
    health = {"blue": True, "green": True}
    weights = {"blue": 100, "green": 0}
    assert pick_backend(weights, health, rng=lambda: 0.99)[0] == "blue"


def test_pick_backend_weighted_green_when_roll_lands_in_green():
    health = {"blue": True, "green": True}
    weights = {"blue": 70, "green": 30}
    # roll = 0.99 * 100 = 99 -> 99 - 70 = 29 < 30 -> green
    assert pick_backend(weights, health, rng=lambda: 0.99)[0] == "green"


def test_pick_backend_skips_unhealthy():
    health = {"blue": True, "green": False}
    weights = {"blue": 0, "green": 100}
    serving, favorite = pick_backend(weights, health, rng=lambda: 0.99)
    assert serving == "blue"      # green is down -> fail over to blue
    assert favorite == "green"    # but green still owns the weight


def test_pick_backend_none_healthy():
    health = {"blue": False, "green": False}
    weights = {"blue": 100, "green": 0}
    assert pick_backend(weights, health, rng=lambda: 0.5)[0] is None


# --------------------------------------------------------------------------- #
# sampling through a stub router                                              #
# --------------------------------------------------------------------------- #
class _RouterStub(BaseHTTPRequestHandler):
    """Pretends to be the router: stamps X-Served-By with ?color=..."""

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        color = "blue" if "color=green" not in self.path else "green"
        body = b"{}"
        self.send_response(200)
        self.send_header("X-Served-By", color)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub_router():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RouterStub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_sample_served_counts_colors(stub_router):
    result = sample_served(stub_router, samples=5)
    assert result["counts"] == {"blue": 5}
    assert result["errors"] == 0


def test_verify_full_shift_passes_for_expected_color(stub_router):
    ok, result = verify_full_shift(stub_router, "blue", samples=4)
    assert ok
    assert result["counts"]["blue"] == 4


def test_verify_full_shift_fails_for_wrong_color(stub_router):
    ok, result = verify_full_shift(stub_router, "green", samples=4)
    assert not ok
    assert result["counts"]["blue"] == 4
