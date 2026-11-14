"""Liveness / readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from akkiabot_server.api.deps import RuntimeDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving HTTP."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(runtime: RuntimeDep) -> JSONResponse:
    """Readiness: database and cache are reachable."""
    checks = await runtime.readiness()
    ok = all(checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", "checks": checks},
        status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
