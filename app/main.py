from __future__ import annotations

from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .context_memory import context_status, get_life_context_snapshot
from .db import fetch_all, init_db
from .orchestrator import (
    accept_reply,
    apply_relation_choice,
    autobiography_edition_detail,
    book_edition_detail,
    chapter_detail,
    compile_autobiography,
    confirm_autobiography_edition,
    confirm_book_edition,
    confirm_chapter,
    create_share,
    create_project,
    delete_photo,
    delete_project,
    export_project,
    generate_chapter,
    generate_initial_event_title,
    get_shared_chapter,
    list_chapters,
    list_autobiography_editions,
    list_book_editions,
    people_catalog,
    reconcile_temporal_evidence,
    require_row,
    adopt_revision_candidate,
    discard_revision_candidate,
    revoke_share,
    revise_chapter,
    save_photo,
    session_detail,
    start_interview,
    timeline_detail,
    update_fact,
    update_project,
    update_timeline_event,
    weave_book,
)
from .schemas import (
    FactUpdate, ProjectCreate, ProjectUpdate, RelationChoice, ReplyCreate,
    RevisionCreate, TimelineEventUpdate,
)
from .vision import analyze_and_store_photo, photo_observation


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    reconcile_temporal_evidence()
    yield


app = FastAPI(title="人生故事 Agent", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.root_dir / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.root_dir / "app" / "static" / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "llm_mode": "mock" if settings.use_mock_llm else "deepseek",
        "model": settings.deepseek_model,
        "voice_enabled": True,
        "voice_mode": "browser_web_speech",
        "server_asr_enabled": False,
        "vision_enabled": settings.vision_enabled,
        "vision_mode": settings.vision_mode,
        "vision_model": settings.vision_model if settings.vision_enabled else None,
    }


@app.get("/api/projects")
def projects() -> list[dict]:
    return fetch_all("SELECT * FROM projects ORDER BY updated_at DESC")


@app.post("/api/projects")
def projects_create(body: ProjectCreate) -> dict:
    return create_project(body.title, body.narrative_person)


@app.get("/api/projects/{project_id}")
def projects_get(project_id: str) -> dict:
    project = require_row("projects", project_id)
    project["photos"] = fetch_all(
        "SELECT * FROM photos WHERE project_id = ? AND deleted_at IS NULL ORDER BY created_at", (project_id,)
    )
    for photo in project["photos"]:
        photo["media_url"] = f"/media/{photo['stored_name']}"
    project["chapters"] = list_chapters(project_id)
    project["timeline"] = timeline_detail(project_id)
    return project


@app.patch("/api/projects/{project_id}")
def projects_update(project_id: str, body: ProjectUpdate) -> dict:
    return update_project(project_id, body.title, body.narrative_person)


@app.get("/api/projects/{project_id}/export")
def projects_export(project_id: str) -> dict:
    return export_project(project_id)


@app.get("/api/projects/{project_id}/memory-context")
def projects_memory_context(project_id: str) -> dict:
    require_row("projects", project_id)
    sessions = fetch_all(
        "SELECT id FROM interview_sessions WHERE project_id = ? ORDER BY created_at", (project_id,)
    )
    return {
        "life_context_snapshot": get_life_context_snapshot(project_id),
        "compression_policy": {
            "trigger_tokens": settings.context_compression_trigger_tokens,
            "target_tokens": settings.context_compression_target_tokens,
            "recent_turns_max": settings.context_recent_turns,
        },
        "sessions": [{"session_id": row["id"], **context_status(row["id"])} for row in sessions],
    }


@app.delete("/api/projects/{project_id}")
def projects_delete(project_id: str) -> dict:
    return delete_project(project_id)


@app.post("/api/projects/{project_id}/photos")
async def photos_create(
    project_id: str,
    image: UploadFile = File(...),
    note: str = Form(default=""),
    title: str = Form(default=""),
) -> dict:
    photo = await save_photo(project_id, image, note, title)
    observation = await analyze_and_store_photo(photo)
    session = start_interview(photo["id"])
    await generate_initial_event_title(session["id"], observation)
    session = session_detail(session["id"])
    return {"photo": photo, "observation": observation, "session": session}


@app.get("/api/photos/{photo_id}/observation")
def photos_observation(photo_id: str) -> dict:
    photo = require_row("photos", photo_id)
    if photo.get("deleted_at"):
        raise HTTPException(status_code=410, detail="这张照片已经删除，但故事文字仍然保留")
    return photo_observation(photo_id) or {"status": "not_analyzed"}


@app.post("/api/photos/{photo_id}/analyze")
async def photos_analyze(photo_id: str) -> dict:
    photo = require_row("photos", photo_id)
    if photo.get("deleted_at"):
        raise HTTPException(status_code=410, detail="这张照片已经删除，但故事文字仍然保留")
    observation = await analyze_and_store_photo(photo)
    session = fetch_all("SELECT id FROM interview_sessions WHERE photo_id = ? LIMIT 1", (photo_id,))
    if session:
        await generate_initial_event_title(session[0]["id"], observation, force=True)
    return observation


@app.delete("/api/photos/{photo_id}")
def photos_delete(photo_id: str, delete_content: bool = False) -> dict:
    return delete_photo(photo_id, delete_content)


@app.post("/api/photos/{photo_id}/start")
def interviews_start(photo_id: str) -> dict:
    return start_interview(photo_id)


@app.get("/api/sessions/{session_id}")
def sessions_get(session_id: str) -> dict:
    return session_detail(session_id)


@app.post("/api/sessions/{session_id}/reply")
async def sessions_reply(session_id: str, body: ReplyCreate) -> dict:
    return await accept_reply(session_id, body.text)


@app.post("/api/sessions/{session_id}/generate")
async def sessions_generate(session_id: str) -> dict:
    return await generate_chapter(session_id)


@app.patch("/api/facts/{fact_id}")
def facts_update(fact_id: str, body: FactUpdate) -> dict:
    return update_fact(fact_id, body.value, body.include_in_book, body.sensitivity, body.event_id)


@app.get("/api/projects/{project_id}/timeline")
def projects_timeline(project_id: str) -> list[dict]:
    return timeline_detail(project_id)


@app.get("/api/projects/{project_id}/people")
async def projects_people(project_id: str) -> dict:
    return await people_catalog(project_id)


@app.patch("/api/timeline-events/{event_id}")
def timeline_events_update(event_id: str, body: TimelineEventUpdate) -> dict:
    return update_timeline_event(event_id, body.model_dump(exclude_unset=True))


@app.get("/api/projects/{project_id}/chapters")
def chapters_list(project_id: str) -> list[dict]:
    return list_chapters(project_id)


@app.get("/api/projects/{project_id}/book-editions")
def book_editions_list(project_id: str) -> list[dict]:
    return list_book_editions(project_id)


@app.get("/api/projects/{project_id}/autobiography-editions")
def autobiography_editions_list(project_id: str) -> list[dict]:
    return list_autobiography_editions(project_id)


@app.post("/api/projects/{project_id}/autobiography/compile")
async def autobiography_compile(project_id: str) -> dict:
    return await compile_autobiography(project_id)


@app.get("/api/autobiography-editions/{edition_id}")
def autobiography_editions_get(edition_id: str) -> dict:
    return autobiography_edition_detail(edition_id)


@app.post("/api/autobiography-editions/{edition_id}/confirm")
def autobiography_editions_confirm(edition_id: str) -> dict:
    return confirm_autobiography_edition(edition_id)


@app.post("/api/projects/{project_id}/weave")
async def projects_weave(project_id: str) -> dict:
    return await weave_book(project_id)


@app.get("/api/book-editions/{edition_id}")
def book_editions_get(edition_id: str) -> dict:
    return book_edition_detail(edition_id)


@app.post("/api/book-editions/{edition_id}/confirm")
def book_editions_confirm(edition_id: str) -> dict:
    return confirm_book_edition(edition_id)


@app.get("/api/chapters/{chapter_id}")
def chapters_get(chapter_id: str) -> dict:
    return chapter_detail(chapter_id)


@app.post("/api/chapters/{chapter_id}/revise")
async def chapters_revise(chapter_id: str, body: RevisionCreate) -> dict:
    return await revise_chapter(chapter_id, body.instruction, body.mode, body.base_candidate_id)


@app.post("/api/chapter-revision-candidates/{candidate_id}/adopt")
def chapter_revision_candidates_adopt(candidate_id: str) -> dict:
    return adopt_revision_candidate(candidate_id)


@app.post("/api/chapter-revision-candidates/{candidate_id}/discard")
def chapter_revision_candidates_discard(candidate_id: str) -> dict:
    return discard_revision_candidate(candidate_id)


@app.post("/api/chapters/{chapter_id}/confirm")
def chapters_confirm(chapter_id: str) -> dict:
    return confirm_chapter(chapter_id)


@app.post("/api/chapters/{chapter_id}/shares")
def shares_create(chapter_id: str) -> dict:
    return create_share(chapter_id)


@app.delete("/api/shares/{share_id}")
def shares_revoke(share_id: str) -> dict:
    return revoke_share(share_id)


@app.get("/api/shares/by-token/{token}")
def shares_get(token: str) -> dict:
    return get_shared_chapter(token)


@app.get("/share/{token}", response_class=HTMLResponse)
def shared_chapter_page(token: str) -> str:
    shared = get_shared_chapter(token)
    title = escape(shared["title"])
    paragraphs = "".join(
        f"<p>{escape(paragraph)}</p>"
        for paragraph in shared["content"].split("\n\n")
        if paragraph.strip()
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 拾光成书</title><style>
body{{margin:0;background:#f2eee5;color:#24302b;font-family:Georgia,'STSong',serif}}
main{{max-width:760px;margin:40px auto;padding:clamp(24px,6vw,64px);background:#fffdf8;border-radius:18px}}
h1{{font-size:32px}}p{{font-size:18px;line-height:2}}small{{color:#6c756f}}
</style></head><body><main><small>拾光成书 · 私密章节 · 第 {shared['version_number']} 版</small>
<h1>{title}</h1>{paragraphs}</main></body></html>"""


@app.post("/api/photos/{photo_id}/relation")
async def photos_relation(photo_id: str, body: RelationChoice) -> dict:
    return await apply_relation_choice(photo_id, body.choice, body.chapter_id, body.source_chapter_id)
