from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Form, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .assets import asset_root
from .comparisons import get_leaderboard, get_next_comparison, record_vote
from .db import close_pool, open_pool
from .settings import get_settings
from .voter import attach_voter_cookie, request_identity
from .settings import ROOT_DIR

app = FastAPI(title="AdvertBench")
templates = Jinja2Templates(directory=ROOT_DIR / "backend" / "templates")
app.mount("/static", StaticFiles(directory=ROOT_DIR / "backend" / "static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().app_base_url, "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class VotePayload(BaseModel):
    winnerSetId: str
    loserSetId: str
    idempotencyKey: str | None = Field(default=None, max_length=128)


@app.on_event("startup")
def startup() -> None:
    open_pool()


@app.on_event("shutdown")
def shutdown() -> None:
    close_pool()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _template_response(request: Request, template: str, context: dict, identity: dict | None = None) -> Response:
    identity = identity or request_identity(request)
    response = templates.TemplateResponse(
        request,
        template,
        {
            **context,
            "active_path": request.url.path,
        },
    )
    return attach_voter_cookie(response, str(identity["voter_id"]))


@app.get("/", response_class=HTMLResponse)
def vote_page(request: Request, status: str | None = None) -> Response:
    identity = request_identity(request)
    leaderboard = get_leaderboard()
    return _template_response(
        request,
        "vote.html",
        {
            "comparison": get_next_comparison(str(identity["voter_hash"])),
            "leaderboard": leaderboard,
            "total_votes": sum(row["matches"] for row in leaderboard) // 2,
            "status": status,
            "idempotency_key": str(uuid4()),
        },
        identity,
    )


@app.post("/vote")
def vote_form(
    request: Request,
    winnerSetId: Annotated[str, Form()],
    loserSetId: Annotated[str, Form()],
    idempotencyKey: Annotated[str | None, Form()] = None,
) -> Response:
    identity = request_identity(request)
    result = record_vote(
        winner_set_id=winnerSetId,
        loser_set_id=loserSetId,
        idempotency_key=idempotencyKey,
        voter_hash=str(identity["voter_hash"]),
        ip_hash=str(identity["ip_hash"]),
        user_agent_hash=str(identity["user_agent_hash"]),
    )
    status = "saved" if result["accepted"] else str(result.get("reason", "rejected"))
    response = RedirectResponse(f"/?status={status}", status_code=303)
    return attach_voter_cookie(response, str(identity["voter_id"]))


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request) -> Response:
    return _template_response(request, "leaderboard.html", {"leaderboard": get_leaderboard()})


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> Response:
    return _template_response(request, "about.html", {})


@app.get("/api/comparisons")
def comparisons(request: Request) -> Response:
    identity = request_identity(request)
    response = JSONResponse(
        {
            "comparison": get_next_comparison(str(identity["voter_hash"])),
            "leaderboard": get_leaderboard(),
        }
    )
    return attach_voter_cookie(response, str(identity["voter_id"]))


@app.get("/api/leaderboard")
def leaderboard() -> dict:
    return {"leaderboard": get_leaderboard()}


@app.post("/api/votes")
def votes(payload: VotePayload, request: Request) -> Response:
    identity = request_identity(request)
    result = record_vote(
        winner_set_id=payload.winnerSetId,
        loser_set_id=payload.loserSetId,
        idempotency_key=payload.idempotencyKey,
        voter_hash=str(identity["voter_hash"]),
        ip_hash=str(identity["ip_hash"]),
        user_agent_hash=str(identity["user_agent_hash"]),
    )
    status = 200 if result["accepted"] else 429 if result.get("reason") == "rate_limited" else 409
    response = JSONResponse(
        {
            "result": result,
            "comparison": get_next_comparison(str(identity["voter_hash"])) if result["accepted"] else None,
            "leaderboard": get_leaderboard(),
        },
        status_code=status,
    )
    return attach_voter_cookie(response, str(identity["voter_id"]))


@app.get("/api/assets/{set_id}/{file_name}")
def assets(set_id: str, file_name: str) -> FileResponse:
    if len(set_id) != 36 or not file_name.endswith(".png") or "/" in file_name or "\\" in file_name:
        raise HTTPException(status_code=400, detail="Invalid asset path")
    path = (asset_root() / set_id / file_name).resolve()
    root = asset_root().resolve()
    if root not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})
