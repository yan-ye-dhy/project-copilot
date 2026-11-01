"""Same-origin browser interface; only public upload limits reach the client."""

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
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
    return JSONResponse(
        {"upload_max_bytes": request.app.state.settings.upload_max_bytes},
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
