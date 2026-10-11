"""FastAPI application."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import (
    admin,
    admin_accounts,
    admin_bmc,
    admin_images,
    auth,
    boot,
    console,
    jobs,
    kvm,
    os_templates,
    servers,
    ssh_keys,
)
from app.config import settings
from app.db import engine

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("doz")

app = FastAPI(
    title="DedicatedOZ",
    description=(
        "Bare-metal hosting control plane. Every provisioning action is an "
        "async job; poll /api/v1/jobs/{id} for progress."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(servers.router)
app.include_router(jobs.router)
app.include_router(ssh_keys.router)
app.include_router(os_templates.router)
app.include_router(console.router)
app.include_router(admin.router)
app.include_router(admin_accounts.router)
app.include_router(admin_bmc.router)
app.include_router(admin_images.router)
app.include_router(boot.router)
app.include_router(kvm.router)


@app.exception_handler(ValueError)
def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Turn domain errors into 400s instead of 500s.

    Service-layer validation (illegal transitions, bad credential refs) raises
    ValueError subclasses; without this they would surface as opaque server
    errors in the portal.
    """
    log.warning("value error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness plus a real database round trip."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        log.error("database health check failed: %s", exc)
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "environment": settings.environment,
        "version": app.version,
    }
