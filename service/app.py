"""Demo service that bgdeploy blue/green deploys.

The service is deliberately small: it reports its identity (version + color),
exposes /healthz + /readyz for liveness/readiness probes, and can be told to
start failing health checks after N seconds (UNHEALTHY_AFTER) so that the
auto-recovery machinery can be demonstrated.
"""
import os
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

COLOR = os.environ.get("APP_COLOR", "blue")
VERSION = os.environ.get("APP_VERSION", "dev")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))
UNHEALTHY_AFTER = float(os.environ.get("UNHEALTHY_AFTER", "0") or 0)

_started_at = time.monotonic()

app = FastAPI(title="bgdeploy demo service", version=VERSION)


def _unhealthy() -> bool:
    """Return True once UNHEALTHY_AFTER seconds have elapsed (fault injection)."""
    return UNHEALTHY_AFTER > 0 and (time.monotonic() - _started_at) > UNHEALTHY_AFTER


@app.get("/")
def root():
    return {
        "service": "bgdeploy-demo",
        "version": VERSION,
        "color": COLOR,
        "healthy": not _unhealthy(),
    }


@app.get("/version")
def version():
    return {"version": VERSION, "color": COLOR, "status": "ok"}


@app.get("/healthz")
def healthz():
    if _unhealthy():
        return JSONResponse(status_code=503, content={"status": "unhealthy"})
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    if _unhealthy():
        return JSONResponse(status_code=503, content={"status": "not ready"})
    return {"status": "ready"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
