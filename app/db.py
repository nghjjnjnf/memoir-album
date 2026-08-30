from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    narrative_person TEXT NOT NULL CHECK(narrative_person IN ('first', 'third')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    user_title TEXT NOT NULL DEFAULT '',
    relation_choice TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS photo_observations (
    id TEXT PRIMARY KEY,
    photo_id TEXT NOT NULL UNIQUE REFERENCES photos(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    exif_json TEXT NOT NULL,
    observations_json TEXT NOT NULL,
    raw_description TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interview_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    turn_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interview_turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('assistant', 'user')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    primary_session_id TEXT NOT NULL UNIQUE REFERENCES interview_sessions(id) ON DELETE CASCADE,
    primary_photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    time_text TEXT NOT NULL DEFAULT '',
    start_year INTEGER,
    end_year INTEGER,
    time_precision TEXT NOT NULL DEFAULT 'unknown',
    time_locked INTEGER NOT NULL DEFAULT 0,
    time_source_type TEXT,
    time_source_id TEXT,
    location TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    needs_chapter_refresh INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_mentions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES interview_sessions(id) ON DELETE CASCADE,
    photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    event_label TEXT NOT NULL DEFAULT '',
    temporal_role TEXT NOT NULL,
    time_text TEXT NOT NULL DEFAULT '',
    start_year INTEGER,
    end_year INTEGER,
    time_precision TEXT NOT NULL DEFAULT 'unknown',
    life_stage TEXT,
    relative_anchor_year INTEGER,
    confidence REAL NOT NULL DEFAULT 1.0,
    linked_event_id TEXT REFERENCES timeline_events(id) ON DELETE CASCADE,
    link_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_type, source_id, raw_text, temporal_role, time_text)
);

CREATE INDEX IF NOT EXISTS idx_event_mentions_project
ON event_mentions(project_id, linked_event_id, created_at);

CREATE TABLE IF NOT EXISTS event_relations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    target_mention_id TEXT NOT NULL REFERENCES event_mentions(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_event_id, target_mention_id, relation_type)
);

CREATE TABLE IF NOT EXISTS event_title_versions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    stage TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    source_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(event_id, version_number)
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
    photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    fact_type TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_turn_id TEXT REFERENCES interview_turns(id),
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    include_in_book INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT REFERENCES memory_facts(id),
    event_id TEXT REFERENCES timeline_events(id),
    event_link_status TEXT NOT NULL DEFAULT 'current_event',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapter_photos (
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    PRIMARY KEY(chapter_id, photo_id)
);

CREATE TABLE IF NOT EXISTS chapter_events (
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    PRIMARY KEY(chapter_id, event_id)
);

CREATE TABLE IF NOT EXISTS chapter_versions (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    narrative_person TEXT NOT NULL,
    content TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    review_json TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chapter_id, version_number)
);

CREATE TABLE IF NOT EXISTS chapter_revision_candidates (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    base_version_id TEXT NOT NULL REFERENCES chapter_versions(id) ON DELETE CASCADE,
    parent_candidate_id TEXT REFERENCES chapter_revision_candidates(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'adopted', 'discarded', 'superseded', 'stale')),
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    content TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    review_json TEXT NOT NULL,
    correction_json TEXT NOT NULL DEFAULT '{}',
    adopted_version_id TEXT REFERENCES chapter_versions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chapter_revision_candidates
ON chapter_revision_candidates(chapter_id, status, created_at);

CREATE TABLE IF NOT EXISTS model_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
    id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES chapter_versions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS book_editions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    edition_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'review_failed')),
    base_snapshot_json TEXT NOT NULL,
    director_plan_json TEXT NOT NULL,
    review_json TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, edition_number)
);

CREATE TABLE IF NOT EXISTS book_edition_chapters (
    edition_id TEXT NOT NULL REFERENCES book_editions(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES chapter_versions(id) ON DELETE CASCADE,
    chapter_order INTEGER NOT NULL,
    change_summary_json TEXT NOT NULL,
    PRIMARY KEY(edition_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS autobiography_editions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    edition_number INTEGER NOT NULL,
    previous_edition_id TEXT REFERENCES autobiography_editions(id),
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'review_failed')),
    narrative_person TEXT NOT NULL CHECK(narrative_person = 'third'),
    scope TEXT NOT NULL CHECK(scope IN ('micro', 'growing', 'full')),
    core_theme TEXT NOT NULL,
    character_portrait TEXT NOT NULL,
    manuscript_json TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    review_json TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, edition_number)
);

CREATE TABLE IF NOT EXISTS people_catalogs (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    source_fingerprint TEXT NOT NULL,
    catalog_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS life_context_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    source_fingerprint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_life_context_snapshot_source
ON life_context_snapshots(project_id, source_fingerprint);

CREATE TABLE IF NOT EXISTS conversation_compactions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    source_turn_count INTEGER NOT NULL,
    source_through_turn_id TEXT REFERENCES interview_turns(id),
    source_token_count INTEGER NOT NULL,
    compressed_token_count INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version_number)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_facts)")}
        if "include_in_book" not in columns:
            conn.execute("ALTER TABLE memory_facts ADD COLUMN include_in_book INTEGER NOT NULL DEFAULT 1")
        if "supersedes" not in columns:
            conn.execute("ALTER TABLE memory_facts ADD COLUMN supersedes TEXT")
        if "event_id" not in columns:
            conn.execute("ALTER TABLE memory_facts ADD COLUMN event_id TEXT")
        if "event_link_status" not in columns:
            conn.execute("ALTER TABLE memory_facts ADD COLUMN event_link_status TEXT NOT NULL DEFAULT 'current_event'")
        model_run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_runs)")}
        if "project_id" not in model_run_columns:
            conn.execute("ALTER TABLE model_runs ADD COLUMN project_id TEXT")
        photo_columns = {row["name"] for row in conn.execute("PRAGMA table_info(photos)")}
        if "deleted_at" not in photo_columns:
            conn.execute("ALTER TABLE photos ADD COLUMN deleted_at TEXT")
        if "user_title" not in photo_columns:
            conn.execute("ALTER TABLE photos ADD COLUMN user_title TEXT NOT NULL DEFAULT ''")
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(timeline_events)")}
        if "time_locked" not in event_columns:
            conn.execute("ALTER TABLE timeline_events ADD COLUMN time_locked INTEGER NOT NULL DEFAULT 0")
        if "time_source_type" not in event_columns:
            conn.execute("ALTER TABLE timeline_events ADD COLUMN time_source_type TEXT")
        if "time_source_id" not in event_columns:
            conn.execute("ALTER TABLE timeline_events ADD COLUMN time_source_id TEXT")
        # 旧测试数据按“一张照片的一次访谈 = 一个初始人生事件”安全回填；
        # 具体年份会在读取时间线时依据已确认事实重新计算。
        sessions = conn.execute(
            """
            SELECT s.id, s.project_id, s.photo_id, s.created_at, p.note, p.original_name
            FROM interview_sessions s
            JOIN photos p ON p.id = s.photo_id
            LEFT JOIN timeline_events te ON te.primary_session_id = s.id
            WHERE te.id IS NULL
            """
        ).fetchall()
        for session in sessions:
            event_id = str(uuid.uuid4())
            title = (session["note"] or "").strip() or f"照片记忆：{session['original_name']}"
            conn.execute(
                """
                INSERT INTO timeline_events
                (id, project_id, primary_session_id, primary_photo_id, title, time_text,
                 start_year, end_year, time_precision, location, summary, status,
                 needs_chapter_refresh, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '', NULL, NULL, 'unknown', '', '', 'draft', 0, ?, ?)
                """,
                (
                    event_id, session["project_id"], session["id"], session["photo_id"],
                    title[:100], session["created_at"], session["created_at"],
                ),
            )
            conn.execute(
                "UPDATE memory_facts SET event_id = ? WHERE session_id = ? AND event_id IS NULL",
                (event_id, session["id"]),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO chapter_events (chapter_id, event_id)
            SELECT cp.chapter_id, te.id
            FROM chapter_photos cp
            JOIN timeline_events te ON te.primary_photo_id = cp.photo_id
            """
        )


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connection() as conn:
        return row_dict(conn.execute(query, params).fetchone())


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def execute(query: str, params: tuple[Any, ...] = ()) -> None:
    with connection() as conn:
        conn.execute(query, params)
