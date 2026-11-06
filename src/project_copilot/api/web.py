"""Same-origin browser interface; only public upload limits reach the client."""

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
router = APIRouter(include_in_schema=False)
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@router.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html", media_type="text/html", headers=_PAGE_HEADERS)


@router.get("/ui/config")
def public_config(request: Request) -> JSONResponse:
    # Do not serialize Settings: it also contains provider credentials, URLs and
    # server paths that the browser neither needs nor should receive.
    settings = request.app.state.settings
    data = {"upload_max_bytes": settings.upload_max_bytes}
    database = request.app.state.database
    if database is not None:
        from sqlalchemy import func, select
        from sqlalchemy.exc import SQLAlchemyError
        from project_copilot.api.access import public_identity, public_mode
        from project_copilot.db.models import AiRequestReservation
        from project_copilot.services.release_control import ReleaseClosed, require_open
        from project_copilot.services.personal_profile import PERSONAL_JOB_LIMIT
        from uuid import UUID
        try:
            if public_mode(settings):
                with database.transaction() as session:
                    principal = public_identity(request, session)
                    release = require_open(session, settings, user_id=principal.id)
                    if release.state == "PERSONAL_DEMO":
                        limits = release.evidence["limits"]
                        now = session.scalar(select(func.clock_timestamp()))
                        count = lambda *where: session.scalar(select(func.count()).select_from(AiRequestReservation).where(*where))
                        data["personal_demo"] = {
                            "expires_at": release.expires_at.isoformat(), "worker_enabled": release.worker_enabled,
                            "remaining_seconds": max(0, (release.expires_at - now).total_seconds()),
                            "job_limit": PERSONAL_JOB_LIMIT,
                            "jobs_used": count(AiRequestReservation.personal_demo_grant_id == UUID(release.evidence["grant_id"])),
                            "occupied_slots": count(AiRequestReservation.user_id == principal.id,
                                                    AiRequestReservation.slot_released_at.is_(None)),
                            **{key: limits[key] for key in ("user_daily_limit", "global_daily_limit", "concurrent_limit")}}
        except (HTTPException, ReleaseClosed, SQLAlchemyError):
            # Public bootstrap stays small. Never leak another user's profile,
            # stale authorization, database diagnostics or credentials.
            pass
    return JSONResponse(
        data,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def install_web(application: FastAPI) -> None:
    """Serve only the shipped asset directory, keeping every API route intact."""
    application.include_router(router)
    application.mount(
        "/ui-assets",
        StaticFiles(directory=WEB_ROOT / "assets", check_dir=False),
        name="ui-assets",
    )
