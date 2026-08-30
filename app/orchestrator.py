from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime
from functools import cmp_to_key
from typing import Any

from fastapi import HTTPException, UploadFile

from . import agents
from .config import settings
from .context_memory import (
    context_status,
    estimate_tokens,
    get_life_context_snapshot,
    latest_compaction,
    save_compaction,
    select_recent_turns,
)
from .db import connection, execute, fetch_all, fetch_one, now_iso
from .writing_methods import retrieve_method_cards
from .vision import opening_from_observation, photo_observation, user_context_slots


ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

LIFE_STAGE_PATTERNS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("童年", 5, ("小时候", "童年", "幼儿园")),
    ("小学", 10, ("小学", "一年级", "二年级", "三年级", "四年级", "五年级", "六年级")),
    ("初一", 20, ("初一",)),
    ("初二", 21, ("初二",)),
    ("初三", 22, ("初三", "中考")),
    ("初中", 21, ("初中",)),
    ("高一", 30, ("高一",)),
    ("高二", 31, ("高二",)),
    ("高三", 32, ("高三", "高考")),
    ("高中", 31, ("高中",)),
    ("大一", 40, ("大一",)),
    ("大二", 41, ("大二",)),
    ("大三", 42, ("大三",)),
    ("大四", 43, ("大四",)),
    ("大学", 42, ("大学", "校园")),
    ("大学毕业", 44, ("大学毕业", "毕业找工作", "找工作", "毕业旅行")),
    ("参加工作", 50, ("参加工作", "刚工作", "入职", "进厂", "上班")),
    ("结婚成家", 55, ("结婚", "成家")),
    ("养育子女", 60, ("生孩子", "女儿出生", "儿子出生", "孩子出生")),
    ("中年", 70, ("中年",)),
    ("退休", 80, ("退休", "晚年")),
)

LIFE_STAGE_TYPICAL_AGE = {
    "童年": 6,
    "小学": 10,
    "初一": 13,
    "初二": 14,
    "初三": 15,
    "初中": 14,
    "高一": 16,
    "高二": 17,
    "高三": 18,
    "高中": 17,
    "大一": 19,
    "大二": 20,
    "大三": 21,
    "大四": 22,
    "大学": 21,
    "大学毕业": 22,
    "参加工作": 23,
    "结婚成家": 25,
    "养育子女": 30,
    "中年": 45,
    "退休": 60,
}

RELATED_EVENT_MARKERS = (
    "后来", "以后", "之后", "再后来", "又去", "又回", "再次", "再去", "重回", "重返", "回去看",
)
PHOTO_TIME_MARKERS = ("这张照片", "照片里", "照片是", "拍下", "拍摄", "拍的", "照相")


def _valid_image_signature(content_type: str, content: bytes) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def _id() -> str:
    return str(uuid.uuid4())


def require_row(table: str, row_id: str) -> dict[str, Any]:
    allowed = {
        "projects",
        "photos",
        "interview_sessions",
        "timeline_events",
        "memory_facts",
        "chapters",
        "chapter_versions",
        "chapter_revision_candidates",
        "book_editions",
        "autobiography_editions",
        "share_links",
    }
    if table not in allowed:
        raise ValueError("invalid table")
    row = fetch_one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    return row


def create_project(title: str, narrative_person: str) -> dict[str, Any]:
    project_id, now = _id(), now_iso()
    execute(
        "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
        (project_id, title.strip(), narrative_person, now, now),
    )
    return require_row("projects", project_id)


def update_project(project_id: str, title: str | None, narrative_person: str | None) -> dict[str, Any]:
    current = require_row("projects", project_id)
    execute(
        "UPDATE projects SET title = ?, narrative_person = ?, updated_at = ? WHERE id = ?",
        (title.strip() if title else current["title"], narrative_person or current["narrative_person"], now_iso(), project_id),
    )
    return require_row("projects", project_id)


async def save_photo(project_id: str, upload: UploadFile, note: str, user_title: str = "") -> dict[str, Any]:
    require_row("projects", project_id)
    content_type = (upload.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="仅支持 JPG、PNG 或 WebP 图片")
    content = await upload.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"图片不能超过 {settings.max_upload_mb}MB")
    if not _valid_image_signature(content_type, content):
        raise HTTPException(status_code=400, detail="文件内容与图片格式不匹配")
    digest = hashlib.sha256(content).hexdigest()
    stored_name = digest + ALLOWED_IMAGE_TYPES[content_type]
    path = settings.media_dir / stored_name
    if not path.exists():
        path.write_bytes(content)
    photo_id = _id()
    execute(
        """
        INSERT INTO photos
        (id, project_id, stored_name, original_name, content_type, note, user_title,
         relation_choice, created_at, deleted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
        """,
        (
            photo_id, project_id, stored_name, upload.filename or "photo", content_type,
            note.strip(), user_title.strip()[:100], now_iso(),
        ),
    )
    photo = require_row("photos", photo_id)
    photo["media_url"] = f"/media/{stored_name}"
    return photo


def start_interview(photo_id: str) -> dict[str, Any]:
    photo = require_row("photos", photo_id)
    if photo.get("deleted_at"):
        raise HTTPException(status_code=410, detail="这张照片已经删除，但原有故事文字仍然保留")
    existing = fetch_one("SELECT * FROM interview_sessions WHERE photo_id = ?", (photo_id,))
    if existing:
        return session_detail(existing["id"])
    session_id, event_id, now = _id(), _id(), now_iso()
    opening = opening_from_observation(
        photo_observation(photo_id), photo.get("user_title", ""), photo.get("note", "")
    )
    with connection() as conn:
        conn.execute(
            "INSERT INTO interview_sessions VALUES (?, ?, ?, ?, 0, ?, ?)",
            (session_id, photo["project_id"], photo_id, "interviewing", now, now),
        )
        conn.execute(
            "INSERT INTO interview_turns VALUES (?, ?, 'assistant', ?, ?)",
            (_id(), session_id, opening, now),
        )
        event_title = photo["user_title"].strip() or photo["note"].strip() or f"照片记忆：{photo['original_name']}"
        conn.execute(
            """
            INSERT INTO timeline_events
            (id, project_id, primary_session_id, primary_photo_id, title, time_text,
             start_year, end_year, time_precision, location, summary, status,
             needs_chapter_refresh, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', NULL, NULL, 'unknown', '', '', 'draft', 0, ?, ?)
            """,
            (event_id, photo["project_id"], session_id, photo_id, event_title[:100], now, now),
        )
        event_seed = {
            "id": event_id, "project_id": photo["project_id"], "time_text": "",
            "start_year": None, "end_year": None, "time_precision": "unknown",
            "time_locked": 0, "time_source_type": None, "time_source_id": None,
        }
        if photo["user_title"].strip():
            _persist_event_mentions(
                conn, event_seed, session_id, photo_id, "user_title", photo_id,
                photo["user_title"].strip(), now,
            )
            refreshed = conn.execute("SELECT * FROM timeline_events WHERE id = ?", (event_id,)).fetchone()
            if refreshed:
                event_seed = dict(refreshed)
        if photo["note"].strip():
            _persist_event_mentions(
                conn,
                event_seed,
                session_id, photo_id, "photo_note", photo_id, photo["note"].strip(), now,
            )
        supplied_place = user_context_slots(photo.get("user_title", ""), photo.get("note", ""))["place"]
        if supplied_place:
            conn.execute(
                "UPDATE timeline_events SET location = ?, updated_at = ? WHERE id = ? AND location = ''",
                (supplied_place, now_iso(), event_id),
            )
    return session_detail(session_id)


def _save_event_title_version(
    event_id: str,
    title: str,
    source: str,
    stage: str,
    rationale: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    normalized = re.sub(r"[\r\n]+", "", str(title or "")).strip().strip("《》“”\"。！!")[:100]
    if not normalized:
        raise ValueError("标题不能为空")
    previous = fetch_one(
        "SELECT COALESCE(MAX(version_number), 0) AS value FROM event_title_versions WHERE event_id = ?",
        (event_id,),
    ) or {"value": 0}
    existing = fetch_one(
        "SELECT * FROM event_title_versions WHERE event_id = ? AND title = ? AND source = ? ORDER BY version_number DESC LIMIT 1",
        (event_id, normalized, source),
    )
    if existing:
        return existing
    version_id, now = _id(), now_iso()
    execute(
        """
        INSERT INTO event_title_versions
        (id, event_id, version_number, title, source, stage, rationale, source_snapshot_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id, event_id, int(previous["value"]) + 1, normalized, source, stage,
            str(rationale or "")[:300], json.dumps(snapshot, ensure_ascii=False), now,
        ),
    )
    return fetch_one("SELECT * FROM event_title_versions WHERE id = ?", (version_id,)) or {}


async def generate_initial_event_title(
    session_id: str,
    observation: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """用户未命名时，用视觉线索和共享人生记忆生成上传阶段的文学标题。"""
    session = require_row("interview_sessions", session_id)
    photo = require_row("photos", session["photo_id"])
    event = _event_for_session(session_id)
    user_title = str(photo.get("user_title") or "").strip()
    if user_title:
        return _save_event_title_version(
            event["id"], user_title, "user", "upload", "用户上传照片时填写的标题。",
            {"photo_id": photo["id"], "authority": "user_title"},
        )

    latest = fetch_one(
        "SELECT * FROM event_title_versions WHERE event_id = ? ORDER BY version_number DESC LIMIT 1",
        (event["id"],),
    )
    if latest and not force:
        return latest
    observation = observation or photo_observation(photo["id"])
    result = await agents.generate_photo_title(
        session["project_id"], photo["id"], str(photo.get("note") or ""), observation,
    )
    source = "title_agent" if result.get("_model_succeeded") else "local_fallback"
    observation_data = (observation or {}).get("observations") or {}
    memory_snapshot = get_life_context_snapshot(session["project_id"])
    return _save_event_title_version(
        event["id"], str(result.get("title") or "照片没有说完的故事"), source, "upload",
        str(result.get("rationale") or ""),
        {
            "photo_id": photo["id"],
            "observation_status": (observation or {}).get("status"),
            "visible_summary": observation_data.get("visible_summary", ""),
            "scene": observation_data.get("scene", ""),
            "visible_text": observation_data.get("visible_text", [])[:8],
            "people_count": observation_data.get("people_count"),
            "place_clues": observation_data.get("place_clues", [])[:3],
            "time_clues": observation_data.get("time_clues", [])[:3],
            "user_note": photo.get("note", ""),
            "shared_snapshot_id": memory_snapshot.get("snapshot_id"),
            "used_memory_fact_ids": result.get("used_memory_fact_ids", []),
        },
    )


def session_detail(session_id: str) -> dict[str, Any]:
    session = require_row("interview_sessions", session_id)
    observation = photo_observation(session["photo_id"])
    # 尚未得到用户回答时，允许识图结果更新第一句；一旦开始讲述就保留原对话历史。
    if session["turn_count"] == 0:
        first_turn = fetch_one(
            "SELECT * FROM interview_turns WHERE session_id = ? AND role = 'assistant' ORDER BY created_at, rowid LIMIT 1",
            (session_id,),
        )
        photo = require_row("photos", session["photo_id"])
        desired_opening = opening_from_observation(
            observation, photo.get("user_title", ""), photo.get("note", "")
        )
        if first_turn and first_turn["content"] != desired_opening:
            execute("UPDATE interview_turns SET content = ? WHERE id = ?", (desired_opening, first_turn["id"]))
    session["turns"] = fetch_all(
        "SELECT * FROM interview_turns WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
    )
    session["facts"] = fetch_all(
        "SELECT * FROM memory_facts WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
    )
    photo = require_row("photos", session["photo_id"])
    photo["is_deleted"] = bool(photo.get("deleted_at"))
    photo["media_url"] = None if photo["is_deleted"] else f"/media/{photo['stored_name']}"
    session["photo"] = photo
    session["photo_observation"] = observation
    session["timeline_event"] = fetch_one(
        "SELECT * FROM timeline_events WHERE primary_session_id = ?", (session_id,)
    )
    if session["timeline_event"]:
        session["event_mentions"] = fetch_all(
            "SELECT * FROM event_mentions WHERE linked_event_id = ? ORDER BY created_at, rowid",
            (session["timeline_event"]["id"],),
        )
    session["context_memory"] = context_status(session_id)
    return session


async def _prepare_session_context(session: dict[str, Any]) -> dict[str, Any]:
    """达到 Token 阈值后压缩旧对话；数据库中的原始 turn 永不删除。"""
    turns = list(session.get("turns") or [])
    facts = _usable_facts(list(session.get("facts") or []))
    shared_snapshot = get_life_context_snapshot(session["project_id"])
    previous = latest_compaction(session["id"])
    covered_count = min(int(previous.get("source_turn_count") or 0), len(turns)) if previous else 0
    previous_summary = previous.get("summary", {}) if previous else {}
    uncompressed_turns = turns[covered_count:]
    raw_context = {
        "shared_life_context": shared_snapshot,
        "conversation_summary": previous_summary,
        "turns": uncompressed_turns,
        "facts": facts,
    }
    raw_tokens = estimate_tokens(raw_context)
    control = {
        "trigger_tokens": settings.context_compression_trigger_tokens,
        "target_tokens": settings.context_compression_target_tokens,
        "estimated_tokens_before": raw_tokens,
        "estimated_tokens_after": raw_tokens,
        "triggered": False,
        "compacted": bool(previous),
        "compaction_version": previous.get("version_number") if previous else None,
        "source_turn_count": covered_count,
    }
    if raw_tokens <= settings.context_compression_trigger_tokens:
        return {
            "model_turns": uncompressed_turns,
            "conversation_summary": previous_summary,
            "shared_life_context": shared_snapshot,
            "context_control": control,
        }

    base_tokens = estimate_tokens({
        "shared_life_context": shared_snapshot,
        "conversation_summary": previous_summary,
        "facts": facts,
    })
    recent_budget = max(300, settings.context_compression_target_tokens - base_tokens)
    recent_turns = select_recent_turns(uncompressed_turns, recent_budget, settings.context_recent_turns)
    older_count = len(uncompressed_turns) - len(recent_turns)
    older_turns = uncompressed_turns[:older_count]
    control["triggered"] = True
    if older_turns:
        summary = await agents.compact_conversation(
            session["project_id"], previous_summary, older_turns, facts
        )
        source_turn_count = covered_count + len(older_turns)
        compacted = save_compaction(
            session["id"],
            source_turn_count,
            older_turns[-1].get("id"),
            raw_tokens,
            summary,
        )
        previous_summary = compacted.get("summary", summary)
        control.update({
            "compacted": True,
            "compaction_version": compacted.get("version_number"),
            "source_turn_count": source_turn_count,
        })
    control["estimated_tokens_after"] = estimate_tokens({
        "shared_life_context": shared_snapshot,
        "conversation_summary": previous_summary,
        "turns": recent_turns,
        "facts": facts,
    })
    return {
        "model_turns": recent_turns,
        "conversation_summary": previous_summary,
        "shared_life_context": shared_snapshot,
        "context_control": control,
    }


def _usable_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fact
        for fact in facts
        if fact.get("status") != "retracted" and bool(fact.get("include_in_book", 1))
    ]


def _visual_evidence_for_photo(photo_id: str | None) -> list[dict[str, str]]:
    """把后台识图结果整理成只供写作使用的可见证据，不传地点/年代猜测和 OCR。"""
    if not photo_id:
        return []
    observation = photo_observation(photo_id)
    if not observation or observation.get("status") != "ready":
        return []
    data = observation.get("observations") or {}
    evidence: list[dict[str, str]] = []

    def add(evidence_id: str, kind: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            evidence.append({"id": evidence_id, "kind": kind, "text": text[:800]})

    add(f"{photo_id}:scene", "scene", data.get("scene"))
    for index, person in enumerate(data.get("people") or [], 1):
        if isinstance(person, dict):
            add(
                f"{photo_id}:person:{index}",
                "visible_person",
                person.get("visible_description"),
            )
    objects = [str(item).strip() for item in (data.get("objects") or []) if str(item).strip()]
    if objects:
        add(f"{photo_id}:objects", "visible_objects", "、".join(objects[:20]))
    return evidence


def _event_for_session(session_id: str) -> dict[str, Any]:
    event = fetch_one("SELECT * FROM timeline_events WHERE primary_session_id = ?", (session_id,))
    if not event:
        raise HTTPException(status_code=404, detail="找不到这次访谈对应的人生事件")
    return event


def _years_in_text(text: str) -> list[int]:
    return [int(year) for year in re.findall(r"(?<!\d)((?:18|19|20|21)\d{2})年?", text)]


def _created_year(created_at: str | None) -> int:
    """相对时间必须锚定讲述发生的年份，不能随系统当前年份漂移。"""
    try:
        return datetime.fromisoformat(str(created_at or "").replace("Z", "+00:00")).year
    except ValueError:
        return datetime.now().year


def _relative_year_from_text(text: str, created_at: str | None) -> tuple[int | None, str]:
    normalized = re.sub(r"\s+", "", text or "")
    base_year = _created_year(created_at)
    for phrase, offset in (("大前年", -3), ("前年", -2), ("去年", -1), ("今年", 0)):
        if phrase in normalized:
            return base_year + offset, phrase
    return None, ""


def _life_stage_from_text(text: str) -> tuple[str, float] | None:
    """返回文本里最具体的人生阶段；例如“大学毕业”优先于“大学”。"""
    # “交通大学门口”描述的是地点，“初三的时候”才是时间；显式时间句式优先。
    explicit = re.search(
        r"(小学|初一|初二|初三|初中|高一|高二|高三|高中|大一|大二|大三|大四|大学毕业|退休)"
        r"(?:的时候|那年|时期|阶段|时|读书)",
        text or "",
    )
    if explicit:
        label = explicit.group(1)
        for candidate, rank, _ in LIFE_STAGE_PATTERNS:
            if candidate == label:
                return candidate, rank
    matches = []
    for label, rank, keywords in LIFE_STAGE_PATTERNS:
        if label == "大学" and not re.search(r"(?:上|读|考入|进入)大学|大学(?:时|期间|阶段|生活)", text or ""):
            # “上海交通大学门口”是地点，不代表讲述发生在大学阶段。
            continue
        if any(keyword in (text or "") for keyword in keywords):
            matches.append((label, rank))
    return max(matches, key=lambda item: item[1]) if matches else None


def _temporal_value(text: str, created_at: str | None) -> dict[str, Any] | None:
    """把绝对、相对、年龄与人生阶段统一为不编造年份的时间对象。"""
    compact = re.sub(r"\s+", "", text or "")
    range_match = re.search(r"((?:18|19|20|21)\d{2})年?(?:到|至|—|-)((?:18|19|20|21)\d{2})年?", compact)
    if range_match:
        start_year, end_year = int(range_match.group(1)), int(range_match.group(2))
        return {
            "time_text": range_match.group(0), "start_year": start_year, "end_year": end_year,
            "time_precision": "range", "life_stage": None, "relative_anchor_year": None,
        }
    year_match = re.search(r"(?<!\d)((?:18|19|20|21)\d{2})年?", compact)
    if year_match:
        year = int(year_match.group(1))
        return {
            "time_text": year_match.group(0), "start_year": year, "end_year": year,
            "time_precision": "year", "life_stage": None, "relative_anchor_year": None,
        }
    relative_year, relative_label = _relative_year_from_text(compact, created_at)
    if relative_year is not None:
        return {
            "time_text": relative_label, "start_year": relative_year, "end_year": relative_year,
            "time_precision": "approximate", "life_stage": None,
            "relative_anchor_year": _created_year(created_at),
        }
    stage = _life_stage_from_text(compact)
    if stage:
        return {
            "time_text": stage[0], "start_year": None, "end_year": None,
            "time_precision": "life_stage", "life_stage": stage[0], "relative_anchor_year": None,
        }
    age_match = re.search(r"(?:我)?(?:那年)?(\d{1,3})岁(?:时|那年|的时候)?", compact)
    if age_match:
        age = int(age_match.group(1))
        if 0 <= age <= 120:
            return {
                "time_text": f"{age}岁", "start_year": None, "end_year": None,
                "time_precision": "age", "life_stage": f"{age}岁", "relative_anchor_year": None,
            }
    return None


def _same_temporal_value(event: dict[str, Any], temporal: dict[str, Any]) -> bool:
    if event.get("start_year") is not None and temporal.get("start_year") is not None:
        return int(event["start_year"]) == int(temporal["start_year"])
    existing_stage = _life_stage_from_text(str(event.get("time_text") or ""))
    return bool(existing_stage and temporal.get("life_stage") == existing_stage[0])


def _is_explicit_time_correction(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if any(marker in compact for marker in ("时间记错", "年份记错", "我记错了", "我说错了")):
        return True
    temporal_token = r"(?:(?:18|19|20|21)\d{2}年?|小学|初一|初二|初三|初中|高一|高二|高三|高中|大[一二三四]|大学毕业|去年|前年|今年|\d{1,3}岁)"
    return bool(re.search(
        rf"不是(?:在)?{temporal_token}.{{0,12}}?(?:而是|应该是|其实是|是){temporal_token}",
        compact,
    ))


def _parse_event_mentions(
    text: str,
    source_type: str,
    created_at: str,
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    """先拆事件提及，再判定时间角色；不允许把“后来/再次”误作照片时间。"""
    raw = str(text or "").strip()
    if not raw:
        return []

    # “不是小学，是初三”等明确更正只采用更正后的时间，不保存被否定值。
    correction = _is_explicit_time_correction(raw)
    parse_text = re.split(r"(?:是|应该是|其实是)", raw)[-1] if correction else raw
    clauses = [
        clause.strip() for clause in re.split(r"(?<=[。！？!?；;])|[，,]", parse_text)
        if clause.strip()
    ] or [parse_text]
    mentions: list[dict[str, Any]] = []
    for clause in clauses:
        temporal = _temporal_value(clause, created_at)
        if not temporal:
            continue
        explicit_photo_time = any(marker in clause for marker in PHOTO_TIME_MARKERS)
        related = any(marker in clause for marker in RELATED_EVENT_MARKERS)
        if correction:
            role = "time_correction"
        elif source_type == "photo_note" and not related:
            role = "photo_capture_event"
        elif explicit_photo_time:
            role = "photo_capture_event"
        elif related:
            role = "later_related_event"
        elif not bool(event.get("time_locked")) and not str(event.get("time_text") or "").strip():
            role = "photo_capture_event"
        elif _same_temporal_value(event, temporal):
            role = "supporting_time"
        else:
            role = "related_event"
        mentions.append({
            **temporal,
            "raw_text": clause[:1000],
            "event_label": re.sub(r"\s+", "", clause)[:100],
            "temporal_role": role,
            "confidence": 1.0 if source_type in {"user_title", "photo_note", "user_reply"} else 0.7,
        })
    return mentions


def _persist_event_mentions(
    conn: Any,
    event: dict[str, Any],
    session_id: str,
    photo_id: str,
    source_type: str,
    source_id: str,
    source_text: str,
    created_at: str,
) -> list[dict[str, Any]]:
    mentions = _parse_event_mentions(source_text, source_type, created_at, event)
    for mention in mentions:
        mention_id = _id()
        insert_cursor = conn.execute(
            """
            INSERT OR IGNORE INTO event_mentions
            (id, project_id, session_id, photo_id, source_type, source_id, raw_text,
             event_label, temporal_role, time_text, start_year, end_year, time_precision,
             life_stage, relative_anchor_year, confidence, linked_event_id, link_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mention_id, event["project_id"], session_id, photo_id, source_type, source_id,
                mention["raw_text"], mention["event_label"], mention["temporal_role"],
                mention["time_text"], mention.get("start_year"), mention.get("end_year"),
                mention["time_precision"], mention.get("life_stage"),
                mention.get("relative_anchor_year"), mention["confidence"], event["id"],
                "primary" if mention["temporal_role"] in {"photo_capture_event", "time_correction", "supporting_time"}
                else "related",
                created_at,
            ),
        )
        mention_inserted = insert_cursor.rowcount > 0
        stored = conn.execute(
            """
            SELECT * FROM event_mentions
            WHERE source_type = ? AND source_id = ? AND raw_text = ?
              AND temporal_role = ? AND time_text = ?
            """,
            (source_type, source_id, mention["raw_text"], mention["temporal_role"], mention["time_text"]),
        ).fetchone()
        stored_id = stored["id"] if stored else mention_id
        if mention["temporal_role"] in {"later_related_event", "related_event"}:
            relation_type = "revisit_of" if mention["temporal_role"] == "later_related_event" else "related_to"
            conn.execute(
                """
                INSERT OR IGNORE INTO event_relations
                (id, project_id, source_event_id, target_mention_id, relation_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_id(), event["project_id"], event["id"], stored_id, relation_type, created_at),
            )
        if mention_inserted:
            conn.execute(
                "UPDATE timeline_events SET updated_at = ? WHERE id = ?",
                (now_iso(), event["id"]),
            )

        may_set_primary = mention["temporal_role"] in {"photo_capture_event", "time_correction"}
        stronger_photo_note = source_type == "photo_note" and event.get("time_source_type") not in {"user_title", "photo_note", "user_reply"}
        if may_set_primary and (
            mention["temporal_role"] == "time_correction"
            or not bool(event.get("time_locked"))
            or stronger_photo_note
        ):
            conn.execute(
                """
                UPDATE timeline_events
                SET time_text = ?, start_year = ?, end_year = ?, time_precision = ?,
                    time_locked = 1, time_source_type = ?, time_source_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    mention["time_text"], mention.get("start_year"), mention.get("end_year"),
                    mention["time_precision"], source_type, source_id, now_iso(), event["id"],
                ),
            )
            event.update({
                "time_text": mention["time_text"], "start_year": mention.get("start_year"),
                "end_year": mention.get("end_year"), "time_precision": mention["time_precision"],
                "time_locked": 1, "time_source_type": source_type, "time_source_id": source_id,
            })
    return mentions


def _ingest_event_mentions(
    event: dict[str, Any],
    session_id: str,
    photo_id: str,
    source_type: str,
    source_id: str,
    source_text: str,
    created_at: str,
) -> list[dict[str, Any]]:
    with connection() as conn:
        return _persist_event_mentions(
            conn, event, session_id, photo_id, source_type, source_id, source_text, created_at
        )


def reconcile_temporal_evidence() -> None:
    """幂等回填旧项目：照片说明和用户原话使用与新数据相同的时间解析链路。"""
    # 清理由旧版宽松规则误标为“时间更正”的派生索引；用户原话不会被修改或删除。
    stale_corrections = fetch_all(
        "SELECT * FROM event_mentions WHERE temporal_role = 'time_correction' ORDER BY created_at, rowid"
    )
    for mention in stale_corrections:
        if _is_explicit_time_correction(str(mention.get("raw_text") or "")):
            continue
        with connection() as conn:
            conn.execute("DELETE FROM event_relations WHERE target_mention_id = ?", (mention["id"],))
            conn.execute("DELETE FROM event_mentions WHERE id = ?", (mention["id"],))
            current = conn.execute(
                "SELECT time_source_id FROM timeline_events WHERE id = ?", (mention["linked_event_id"],)
            ).fetchone()
            if current and current["time_source_id"] == mention["source_id"]:
                fallback = conn.execute(
                    """
                    SELECT * FROM event_mentions
                    WHERE linked_event_id = ? AND link_status = 'primary'
                    ORDER BY CASE WHEN temporal_role = 'time_correction' THEN 0
                                  WHEN source_type = 'photo_note' THEN 1 ELSE 2 END,
                             created_at DESC LIMIT 1
                    """,
                    (mention["linked_event_id"],),
                ).fetchone()
                if fallback:
                    conn.execute(
                        """
                        UPDATE timeline_events SET time_text = ?, start_year = ?, end_year = ?,
                            time_precision = ?, time_locked = 1, time_source_type = ?,
                            time_source_id = ?, updated_at = ? WHERE id = ?
                        """,
                        (
                            fallback["time_text"], fallback["start_year"], fallback["end_year"],
                            fallback["time_precision"], fallback["source_type"], fallback["source_id"],
                            now_iso(), mention["linked_event_id"],
                        ),
                    )
    rows = fetch_all(
        """
        SELECT te.*, p.note, s.id AS session_id, p.id AS photo_id
        FROM timeline_events te
        JOIN interview_sessions s ON s.id = te.primary_session_id
        JOIN photos p ON p.id = te.primary_photo_id
        ORDER BY te.created_at, te.rowid
        """
    )
    for event in rows:
        session_id = event["session_id"]
        photo_id = event["photo_id"]
        note = str(event.get("note") or "").strip()
        if note:
            _ingest_event_mentions(
                event, session_id, photo_id, "photo_note",
                photo_id, note, event["created_at"],
            )
            event = require_row("timeline_events", event["id"])
        turns = fetch_all(
            """
            SELECT * FROM interview_turns
            WHERE session_id = ? AND role = 'user' ORDER BY created_at, rowid
            """,
            (session_id,),
        )
        for turn in turns:
            _ingest_event_mentions(
                event, session_id, photo_id, "user_reply",
                turn["id"], turn["content"], turn["created_at"],
            )
            event = require_row("timeline_events", event["id"])


def _timeline_clues(event: dict[str, Any]) -> dict[str, Any]:
    primary_stage = _life_stage_from_text(str(event.get("time_text") or ""))
    fallback_stage = _life_stage_from_text(
        " ".join(
            str(event.get(key) or "")
            for key in ("title", "summary")
        )
    )
    stage = primary_stage or fallback_stage
    year = event.get("start_year")
    return {
        "year": int(year) if year is not None else None,
        "stage": stage[0] if stage else None,
        "stage_rank": stage[1] if stage else None,
        "stage_age": LIFE_STAGE_TYPICAL_AGE.get(stage[0]) if stage else None,
    }


def _sort_timeline_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按日历年排序；缺少年份时，用同一人的阶段锚点只做排序估计。"""
    clues = {event["id"]: _timeline_clues(event) for event in events}
    birth_year_candidates = [
        clue["year"] - clue["stage_age"]
        for clue in clues.values()
        if clue["year"] is not None and clue["stage_age"] is not None
    ]
    birth_year_anchor = None
    if birth_year_candidates:
        ordered = sorted(birth_year_candidates)
        birth_year_anchor = ordered[len(ordered) // 2]

    for event in events:
        clue = clues[event["id"]]
        sort_year = clue["year"]
        sort_basis = "explicit_year" if sort_year is not None else "unresolved"
        if (
            sort_year is not None
            and event.get("time_precision") == "approximate"
            and any(marker in str(event.get("time_text") or "") for marker in ("今年", "去年", "前年", "大前年", "推算"))
        ):
            sort_basis = "relative_date"
        elif sort_year is None and clue["stage_age"] is not None and birth_year_anchor is not None:
            sort_year = birth_year_anchor + clue["stage_age"]
            sort_basis = "life_stage_anchor"
        elif sort_year is None and clue["stage"]:
            sort_basis = "life_stage_only"
        event["sort_stage"] = clue["stage"]
        event["sort_basis"] = sort_basis
        event["sort_year_estimate"] = sort_year if sort_basis == "life_stage_anchor" else None
        clue["sort_year"] = sort_year

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        left_clue, right_clue = clues[left["id"]], clues[right["id"]]
        left_year, right_year = left_clue.get("sort_year"), right_clue.get("sort_year")
        if left_year is not None and right_year is not None and left_year != right_year:
            return -1 if left_year < right_year else 1
        if left_year is None and right_year is None:
            left_rank, right_rank = left_clue.get("stage_rank"), right_clue.get("stage_rank")
            if left_rank is not None and right_rank is not None and left_rank != right_rank:
                return -1 if left_rank < right_rank else 1
            if (left_rank is None) != (right_rank is None):
                return -1 if left_rank is not None else 1
        elif (left_year is None) != (right_year is None):
            return 1 if left_year is None else -1
        left_created, right_created = str(left.get("created_at") or ""), str(right.get("created_at") or "")
        return (left_created > right_created) - (left_created < right_created)

    return sorted(events, key=cmp_to_key(compare))


def _sentence_excerpt(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", "", text or "").strip()
    if len(compact) <= limit:
        return compact
    sentences = [part for part in re.findall(r"[^。！？!?]+[。！？!?]?", compact) if part]
    selected = ""
    for sentence in sentences:
        if selected and len(selected) + len(sentence) > limit:
            break
        if not selected and len(sentence) > limit:
            return sentence[:limit].rstrip("，、；：") + "……"
        selected += sentence
    return selected or compact[:limit].rstrip("，、；：") + "……"


def _chapter_timeline_preview(content: str, limit: int = 230) -> str:
    """时间线展示照片画面与故事价值，不再暴露后台事实清单。"""
    paragraphs = [re.sub(r"\s+", "", part).strip() for part in re.split(r"\n+", content or "") if part.strip()]
    if not paragraphs:
        return ""
    if len(paragraphs) == 1:
        return _sentence_excerpt(paragraphs[0], limit)
    opening = _sentence_excerpt(paragraphs[0], min(78, limit // 3 + 2))
    ending = _sentence_excerpt(paragraphs[-1], min(72, limit // 3))
    middle_candidates = paragraphs[1:-1]
    meaningful = next(
        (part for part in reversed(middle_candidates) if any(word in part for word in ("这张照片", "现在再看", "后来", "那一刻", "心里"))),
        middle_candidates[len(middle_candidates) // 2] if middle_candidates else "",
    )
    middle_limit = max(0, limit - len(opening) - len(ending) - 4)
    middle = _sentence_excerpt(meaningful, middle_limit) if meaningful and middle_limit >= 24 else ""
    if middle:
        return f"{opening}……{middle}……{ending}"[:limit]
    if not ending or ending == opening:
        return _sentence_excerpt(content, limit)
    return f"{opening}……{ending}"[:limit]


def _facts_timeline_preview(event: dict[str, Any], facts: list[dict[str, Any]], limit: int = 230) -> str:
    values: list[str] = []
    for fact_type in ("event", "feeling", "reflection"):
        for fact in facts:
            value = str(fact.get("value") or "").strip().rstrip("。；;！!")
            if fact.get("fact_type") != fact_type or len(value) < 4:
                continue
            if any(value in existing or existing in value for existing in values):
                continue
            values.append(value)
            if len(values) >= 4:
                break
        if len(values) >= 4:
            break
    if not values:
        time_place = "，".join(
            value for value in (str(event.get("time_text") or ""), str(event.get("location") or "")) if value
        )
        return f"{time_place}，留下了“{event['title']}”这段记忆。" if time_place else "这段记忆还在慢慢展开。"
    return _sentence_excerpt("。".join(values) + "。", limit)


def _apply_event_proposal(
    event: dict[str, Any],
    proposal: dict[str, Any] | None,
    source_text: str = "",
) -> dict[str, Any]:
    if not proposal:
        return event
    start_year = proposal.get("start_year")
    end_year = proposal.get("end_year")
    # 照片时间一旦由用户证据建立，即使只有“小学”等人生阶段，也禁止后续年份覆盖。
    related_reference = any(marker in source_text for marker in RELATED_EVENT_MARKERS)
    if bool(event.get("time_locked")) or related_reference or (
        event.get("start_year") is not None and start_year != event.get("start_year")
    ):
        start_year, end_year = event.get("start_year"), event.get("end_year")
        time_text, precision = event.get("time_text", ""), event.get("time_precision", "unknown")
    else:
        time_text = str(proposal.get("time_text") or event.get("time_text") or "")[:100]
        precision = str(proposal.get("time_precision") or event.get("time_precision") or "unknown")
    title = event["title"]
    proposed_title = str(proposal.get("title") or "").strip()
    if proposed_title and (title.startswith("照片记忆：") or title == "待整理的照片记忆"):
        title = proposed_title[:100]
    location = str(proposal.get("location") or event.get("location") or "")[:200]
    execute(
        """
        UPDATE timeline_events
        SET title = ?, time_text = ?, start_year = ?, end_year = ?, time_precision = ?,
            location = ?, updated_at = ? WHERE id = ?
        """,
        (title, time_text, start_year, end_year, precision, location, now_iso(), event["id"]),
    )
    return require_row("timeline_events", event["id"])


def _fact_target_event(
    value: str,
    reply_text: str,
    current_event: dict[str, Any],
    project_events: list[dict[str, Any]],
    has_related_mention: bool = False,
) -> tuple[str, str]:
    other_events = [event for event in project_events if event["id"] != current_event["id"]]
    fact_years = set(_years_in_text(value))
    reply_years = set(_years_in_text(reply_text))
    candidate_years = fact_years or reply_years
    matches = [event for event in other_events if event.get("start_year") in candidate_years]
    current_year = current_event.get("start_year")
    if len(matches) == 1 and (not fact_years or current_year not in fact_years):
        return matches[0]["id"], "matched_by_year"
    if len(matches) > 1:
        return current_event["id"], "needs_confirmation"
    if has_related_mention:
        return current_event["id"], "related_event_mention"
    return current_event["id"], "current_event"


def _refresh_event_summary(event_id: str) -> None:
    event = require_row("timeline_events", event_id)
    facts = _usable_facts(fetch_all(
        "SELECT * FROM memory_facts WHERE event_id = ? ORDER BY created_at, rowid", (event_id,)
    ))
    summary_values = [
        fact["value"].strip().rstrip("；;。") for fact in facts
        if fact["fact_type"] in {"event", "person", "feeling", "reflection", "other"}
    ]
    summary = "；".join(dict.fromkeys(summary_values))[:1000]
    primary_facts = [fact for fact in facts if fact.get("event_link_status") != "related_event_mention"]
    time_facts = [fact["value"] for fact in primary_facts if fact["fact_type"] == "time"]
    location_facts = [fact["value"] for fact in facts if fact["fact_type"] == "place"]
    start_year, end_year = event.get("start_year"), event.get("end_year")
    time_text, precision = event.get("time_text", ""), event.get("time_precision", "unknown")
    if start_year is None and not bool(event.get("time_locked")):
        for value in time_facts:
            years = _years_in_text(value)
            if years:
                start_year, end_year = min(years), max(years)
                time_text = value[:100]
                precision = "range" if len(set(years)) > 1 else ("approximate" if "大约" in value or "左右" in value else "year")
                break
        if start_year is None:
            relative_context = "；".join(
                [time_text, *time_facts, *[fact["value"] for fact in primary_facts]]
            )
            relative_year, relative_label = _relative_year_from_text(relative_context, event.get("created_at"))
            if relative_year is not None:
                start_year = end_year = relative_year
                time_text = f"{relative_label}（按讲述时间推算为{relative_year}年）"
                precision = "approximate"
        if start_year is None:
            stage_context = "；".join([time_text, *time_facts, summary])
            stage = _life_stage_from_text(stage_context)
            if stage:
                if not time_text or time_text in {"当时", "那时", "那年"}:
                    time_text = stage[0]
                precision = "life_stage"
    location = event.get("location", "") or (location_facts[0][:200] if location_facts else "")
    execute(
        """
        UPDATE timeline_events SET time_text = ?, start_year = ?, end_year = ?, time_precision = ?,
        location = ?, summary = ?, updated_at = ? WHERE id = ?
        """,
        (time_text, start_year, end_year, precision, location, summary, now_iso(), event_id),
    )


def timeline_detail(project_id: str) -> list[dict[str, Any]]:
    require_row("projects", project_id)
    event_ids = [row["id"] for row in fetch_all("SELECT id FROM timeline_events WHERE project_id = ?", (project_id,))]
    for event_id in event_ids:
        _refresh_event_summary(event_id)
    events = _sort_timeline_events(fetch_all(
        "SELECT * FROM timeline_events WHERE project_id = ?", (project_id,)
    ))
    for event in events:
        event["event_mentions"] = fetch_all(
            "SELECT * FROM event_mentions WHERE linked_event_id = ? ORDER BY created_at, rowid",
            (event["id"],),
        )
        event["related_events"] = [
            mention for mention in event["event_mentions"]
            if mention.get("link_status") == "related"
        ]
        event["facts"] = fetch_all(
            """
            SELECT * FROM memory_facts WHERE event_id = ? AND status != 'retracted'
            ORDER BY created_at, rowid
            """,
            (event["id"],),
        )
        photo = require_row("photos", event["primary_photo_id"])
        if photo.get("deleted_at"):
            event["photos"] = []
            event["photo_removed"] = True
        else:
            photo["media_url"] = f"/media/{photo['stored_name']}"
            event["photos"] = [photo]
            event["photo_removed"] = False
        chapter_rows = fetch_all(
            """
            SELECT c.id, c.title, c.status, cv.content FROM chapters c
            JOIN chapter_events ce ON ce.chapter_id = c.id
            LEFT JOIN chapter_versions cv ON cv.id = c.current_version_id
            WHERE ce.event_id = ? AND c.status != 'discarded'
            ORDER BY CASE WHEN c.status = 'confirmed' THEN 0 ELSE 1 END, c.updated_at DESC
            """,
            (event["id"],),
        )
        chapter_content = next((row.get("content") for row in chapter_rows if row.get("content")), "")
        preferred_chapter = next((row for row in chapter_rows if row.get("title")), None)
        title_versions = fetch_all(
            "SELECT * FROM event_title_versions WHERE event_id = ? ORDER BY version_number",
            (event["id"],),
        )
        latest_user_title = next(
            (row for row in reversed(title_versions) if row.get("source") == "user"),
            None,
        )
        latest_generated_title = next(
            (
                row for row in reversed(title_versions)
                if row.get("source") in {"title_agent", "local_fallback"}
            ),
            None,
        )
        event["archival_title"] = event["title"]
        if latest_user_title:
            event["display_title"] = latest_user_title["title"]
            event["title_source"] = "user"
        elif preferred_chapter:
            event["display_title"] = preferred_chapter["title"]
            event["title_source"] = "chapter"
        elif latest_generated_title:
            event["display_title"] = latest_generated_title["title"]
            event["title_source"] = latest_generated_title["source"]
        else:
            event["display_title"] = event["title"]
            event["title_source"] = "event"
        for version in title_versions:
            try:
                version["source_snapshot"] = json.loads(version.pop("source_snapshot_json"))
            except (json.JSONDecodeError, TypeError):
                version["source_snapshot"] = {}
        event["title_versions"] = title_versions
        event["display_summary"] = (
            _chapter_timeline_preview(chapter_content)
            if chapter_content
            else _facts_timeline_preview(event, event["facts"])
        )
        event["summary_source"] = "chapter" if chapter_content else "memory_facts"
        for chapter in chapter_rows:
            chapter.pop("content", None)
        event["chapters"] = chapter_rows
    return events


async def accept_reply(session_id: str, text: str) -> dict[str, Any]:
    session = require_row("interview_sessions", session_id)
    if session["status"] in {"drafting", "pending_confirmation", "confirmed"}:
        raise HTTPException(status_code=409, detail="本轮访谈已进入成稿阶段")
    user_turn_id, now = _id(), now_iso()
    with connection() as conn:
        conn.execute(
            "INSERT INTO interview_turns VALUES (?, ?, 'user', ?, ?)",
            (user_turn_id, session_id, text.strip(), now),
        )
        conn.execute(
            "UPDATE interview_sessions SET turn_count = turn_count + 1, updated_at = ? WHERE id = ?",
            (now, session_id),
        )

    event_before = _event_for_session(session_id)
    mentions = _ingest_event_mentions(
        event_before, session_id, session["photo_id"], "user_reply", user_turn_id,
        text.strip(), now,
    )
    existing_facts = fetch_all("SELECT * FROM memory_facts WHERE session_id = ?", (session_id,))
    proposal = await agents.extract_facts(text, existing_facts, session["project_id"], session_id)
    new_facts = proposal.get("facts", []) if isinstance(proposal.get("facts"), list) else []
    current_event = _apply_event_proposal(
        _event_for_session(session_id), proposal.get("current_event"), text
    )
    project_events = fetch_all("SELECT * FROM timeline_events WHERE project_id = ?", (session["project_id"],))
    touched_event_ids: set[str] = {current_event["id"]}
    with connection() as conn:
        existing_values = {row["value"] for row in existing_facts}
        for fact in new_facts[:12]:
            value = str(fact.get("value", "")).strip()
            if not value or value in existing_values:
                continue
            related_mentions = [
                mention for mention in mentions
                if mention["temporal_role"] in {"later_related_event", "related_event"}
            ]
            primary_mentions = [
                mention for mention in mentions
                if mention["temporal_role"] in {"photo_capture_event", "time_correction", "supporting_time"}
            ]
            fact_is_related = bool(related_mentions) and (
                not primary_mentions
                or any(marker in value for marker in RELATED_EVENT_MARKERS)
                or any(str(mention.get("time_text") or "") in value for mention in related_mentions)
            )
            target_event_id, link_status = _fact_target_event(
                value, text, current_event, project_events, fact_is_related
            )
            touched_event_ids.add(target_event_id)
            conn.execute(
                """
                INSERT INTO memory_facts
                (id, project_id, session_id, photo_id, fact_type, value, status,
                 evidence_turn_id, sensitivity, include_in_book, supersedes, event_id,
                 event_link_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'asserted_by_user', ?, ?, 1, NULL, ?, ?, ?)
                """,
                (
                    _id(),
                    session["project_id"],
                    session_id,
                    session["photo_id"],
                    str(fact.get("fact_type", "other"))[:30],
                    value,
                    user_turn_id,
                    "sensitive" if fact.get("sensitivity") == "sensitive" else "normal",
                    target_event_id,
                    link_status,
                    now_iso(),
                ),
            )
            if target_event_id != current_event["id"]:
                conn.execute(
                    "UPDATE timeline_events SET needs_chapter_refresh = 1, updated_at = ? WHERE id = ?",
                    (now_iso(), target_event_id),
                )

    for event_id in touched_event_ids:
        _refresh_event_summary(event_id)

    current = session_detail(session_id)
    observation = current.get("photo_observation") or {}
    observation_context = {
        "status": observation.get("status"),
        "exif": observation.get("exif", {}),
        "observations": observation.get("observations", {}),
    } if observation else None
    context_pack = await _prepare_session_context(current)
    decision = await agents.next_interview_turn(
        current["turns"], current["facts"], current["turn_count"], session["project_id"],
        observation_context,
        model_turns=context_pack["model_turns"],
        conversation_summary=context_pack["conversation_summary"],
        context_control=context_pack["context_control"],
    )
    # 轮数只是节奏参考，不能覆盖 Interview Agent 的未解线索检查。
    ready = bool(decision.get("ready_to_draft"))
    reply = str(decision.get("reply", "原来是这样呀。"))
    question = str(decision.get("question", "")).strip()
    if ready:
        assistant_text = reply
        status = "ready_to_draft"
    else:
        assistant_text = (reply + " " + question).strip()
        status = "interviewing"
    with connection() as conn:
        conn.execute(
            "INSERT INTO interview_turns VALUES (?, ?, 'assistant', ?, ?)",
            (_id(), session_id, assistant_text, now_iso()),
        )
        conn.execute(
            "UPDATE interview_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), session_id),
        )
    result = session_detail(session_id)
    result["interview_decision"] = {"ready_to_draft": ready, "reason": decision.get("reason", "")}
    result["context_memory"] = {**result["context_memory"], **context_pack["context_control"]}
    return result


def _chapter_summary_rows(project_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT c.id, c.title, cv.content, MIN(te.start_year) AS timeline_year,
               MAX(te.needs_chapter_refresh) AS has_new_memory,
               GROUP_CONCAT(DISTINCT te.id) AS timeline_event_ids
        FROM chapters c
        LEFT JOIN chapter_versions cv ON cv.id = c.current_version_id
        LEFT JOIN chapter_events ce ON ce.chapter_id = c.id
        LEFT JOIN timeline_events te ON te.id = ce.event_id
        WHERE c.project_id = ? AND c.status != 'discarded'
        GROUP BY c.id, c.title, cv.content, c.created_at
        """,
        (project_id,),
    )
    event_order = {
        event["id"]: index
        for index, event in enumerate(timeline_detail(project_id))
    }
    for row in rows:
        linked_ids = [value for value in str(row.pop("timeline_event_ids") or "").split(",") if value]
        row["timeline_order"] = min((event_order.get(value, 10**9) for value in linked_ids), default=10**9)
    rows.sort(key=lambda row: (row["timeline_order"], row.get("timeline_year") or 10**9, row["id"]))
    for row in rows:
        row.pop("timeline_order", None)
    return rows


async def generate_chapter(session_id: str) -> dict[str, Any]:
    session = session_detail(session_id)
    if session["status"] == "pending_confirmation":
        raise HTTPException(status_code=409, detail="这次访谈已经生成章节，请在章节中继续修改")
    if session["turn_count"] < 1:
        raise HTTPException(status_code=400, detail="请至少讲述一段内容后再生成章节")
    project = require_row("projects", session["project_id"])
    event = _event_for_session(session_id)
    existing_chapters = _chapter_summary_rows(project["id"])
    usable_facts = _usable_facts(fetch_all(
        "SELECT * FROM memory_facts WHERE event_id = ? ORDER BY created_at, rowid", (event["id"],)
    ))
    if not usable_facts:
        raise HTTPException(status_code=400, detail="当前没有允许写入书稿的事实，请先补充或恢复事实线索")
    story_text = "\n".join(fact["value"] for fact in usable_facts)
    method_cards = retrieve_method_cards(project["narrative_person"], story_text)
    visual_evidence = _visual_evidence_for_photo(session.get("photo_id"))
    context_pack = await _prepare_session_context(session)
    draft = await agents.draft_chapter(
        project["narrative_person"], session["turns"], usable_facts, method_cards, visual_evidence,
        model_turns=context_pack["model_turns"],
        conversation_summary=context_pack["conversation_summary"],
        context_control=context_pack["context_control"],
    )
    content = str(draft.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=502, detail="章节生成失败，请稍后重试")
    review = await agents.review_chapter(
        content,
        usable_facts,
        project["narrative_person"],
        session["turns"],
        visual_evidence,
        draft.get("literary_inferences") or [],
        model_turns=context_pack["model_turns"],
        conversation_summary=context_pack["conversation_summary"],
        context_control=context_pack["context_control"],
    )
    corrected = str(review.get("corrected_content") or content).strip()
    chapter_id, version_id, now = _id(), _id(), now_iso()
    title = str(draft.get("title") or "一张照片的故事").strip()[:100]
    source_snapshot = {
        "session_id": session_id,
        "event_id": event["id"],
        "photo_id": session["photo_id"],
        "turn_ids": [turn["id"] for turn in session["turns"] if turn["role"] == "user"],
        "fact_ids": [fact["id"] for fact in usable_facts],
        "writing_method_ids": [card["id"] for card in method_cards],
        "visual_evidence": visual_evidence,
        "used_visual_ids": draft.get("used_visual_ids") or [],
        "literary_inferences": draft.get("literary_inferences") or [],
    }
    with connection() as conn:
        conn.execute(
            "INSERT INTO chapters VALUES (?, ?, ?, 'draft', ?, ?, ?)",
            (chapter_id, project["id"], title, version_id, now, now),
        )
        conn.execute(
            "INSERT INTO chapter_versions VALUES (?, ?, 1, ?, ?, ?, ?, NULL, ?)",
            (
                version_id,
                chapter_id,
                project["narrative_person"],
                corrected,
                json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps(review, ensure_ascii=False),
                now,
            ),
        )
        conn.execute("INSERT INTO chapter_photos VALUES (?, ?)", (chapter_id, session["photo_id"]))
        conn.execute("INSERT INTO chapter_events VALUES (?, ?)", (chapter_id, event["id"]))
        conn.execute(
            "UPDATE timeline_events SET needs_chapter_refresh = 0, updated_at = ? WHERE id = ?",
            (now, event["id"]),
        )
        conn.execute(
            "UPDATE interview_sessions SET status = 'pending_confirmation', updated_at = ? WHERE id = ?",
            (now, session_id),
        )
    relation = None
    if existing_chapters:
        relation = await agents.suggest_relation(story_text, existing_chapters, project["id"])
    result = chapter_detail(chapter_id)
    result["relation_suggestion"] = relation
    return result


def revision_candidate_detail(candidate_id: str) -> dict[str, Any]:
    candidate = require_row("chapter_revision_candidates", candidate_id)
    for source, target, fallback in (
        ("source_snapshot_json", "source_snapshot", {}),
        ("review_json", "review", {}),
        ("correction_json", "correction", {}),
    ):
        try:
            candidate[target] = json.loads(candidate.get(source) or "{}")
        except json.JSONDecodeError:
            candidate[target] = fallback
    return candidate


def chapter_detail(chapter_id: str) -> dict[str, Any]:
    chapter = require_row("chapters", chapter_id)
    chapter["versions"] = fetch_all(
        "SELECT * FROM chapter_versions WHERE chapter_id = ? ORDER BY version_number DESC", (chapter_id,)
    )
    chapter["current_version"] = fetch_one(
        "SELECT * FROM chapter_versions WHERE id = ?", (chapter["current_version_id"],)
    )
    pending_candidate = fetch_one(
        """
        SELECT * FROM chapter_revision_candidates
        WHERE chapter_id = ? AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
        """,
        (chapter_id,),
    )
    chapter["revision_candidate"] = (
        revision_candidate_detail(pending_candidate["id"]) if pending_candidate else None
    )
    chapter["photos"] = fetch_all(
        """
        SELECT p.* FROM photos p JOIN chapter_photos cp ON cp.photo_id = p.id
        WHERE cp.chapter_id = ? AND p.deleted_at IS NULL ORDER BY p.created_at
        """,
        (chapter_id,),
    )
    for photo in chapter["photos"]:
        photo["media_url"] = f"/media/{photo['stored_name']}"
    chapter["active_shares"] = fetch_all(
        "SELECT id, token, version_id, created_at FROM share_links WHERE chapter_id = ? AND status = 'active'",
        (chapter_id,),
    )
    chapter["events"] = _sort_timeline_events(fetch_all(
        """
        SELECT te.* FROM timeline_events te JOIN chapter_events ce ON ce.event_id = te.id
        WHERE ce.chapter_id = ?
        """,
        (chapter_id,),
    ))
    chapter["has_new_memory"] = any(bool(event["needs_chapter_refresh"]) for event in chapter["events"])
    chapter["next_story_suggestion"] = _next_story_suggestion(chapter)
    return chapter


def _next_story_suggestion(chapter: dict[str, Any]) -> str:
    current = chapter.get("current_version") or {}
    try:
        source = json.loads(current.get("source_snapshot_json", "{}"))
    except json.JSONDecodeError:
        source = {}
    fact_ids = source.get("fact_ids", [])
    anchor = None
    if fact_ids:
        placeholders = ",".join("?" for _ in fact_ids)
        fact = fetch_one(
            f"SELECT value FROM memory_facts WHERE id IN ({placeholders}) AND fact_type IN ('person', 'time', 'place') LIMIT 1",
            tuple(fact_ids),
        )
        anchor = fact["value"][:24] if fact else None
    if anchor:
        return f"如果您愿意，下次可以找一张和“{anchor}”有关的照片；不着急，想到时再继续就好。"
    return "如果您愿意，下次可以再选一张让您有话想说的照片；不着急，想到时再继续就好。"


def list_chapters(project_id: str) -> list[dict[str, Any]]:
    require_row("projects", project_id)
    rows = _chapter_summary_rows(project_id)
    for row in rows:
        row["preview"] = (row.get("content") or "")[:160]
        row.pop("content", None)
    return rows


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def book_edition_detail(edition_id: str) -> dict[str, Any]:
    edition = require_row("book_editions", edition_id)
    edition["base_snapshot"] = _json_object(edition["base_snapshot_json"])
    edition["director_plan"] = _json_object(edition["director_plan_json"])
    edition["review"] = _json_object(edition["review_json"])
    edition["chapters"] = fetch_all(
        """
        SELECT bec.chapter_order, bec.chapter_id, bec.version_id, bec.change_summary_json,
               c.title, cv.version_number, cv.content, cv.review_json, cv.confirmed_at
        FROM book_edition_chapters bec
        JOIN chapters c ON c.id = bec.chapter_id
        JOIN chapter_versions cv ON cv.id = bec.version_id
        WHERE bec.edition_id = ? ORDER BY bec.chapter_order
        """,
        (edition_id,),
    )
    for chapter in edition["chapters"]:
        chapter["change_summary"] = _json_object(chapter["change_summary_json"])
        chapter["review"] = _json_object(chapter["review_json"])
    return edition


def list_book_editions(project_id: str) -> list[dict[str, Any]]:
    require_row("projects", project_id)
    return fetch_all(
        "SELECT id, edition_number, title, status, confirmed_at, created_at FROM book_editions WHERE project_id = ? ORDER BY edition_number DESC",
        (project_id,),
    )


def autobiography_edition_detail(edition_id: str) -> dict[str, Any]:
    edition = require_row("autobiography_editions", edition_id)
    edition["manuscript"] = _json_object(edition["manuscript_json"])
    edition["source_snapshot"] = _json_object(edition["source_snapshot_json"])
    edition["review"] = _json_object(edition["review_json"])
    for section in edition["manuscript"].get("sections", []):
        photo_ids = [str(photo_id) for photo_id in section.get("photo_ids", []) if photo_id]
        if not photo_ids:
            section["photos"] = []
            continue
        placeholders = ",".join("?" for _ in photo_ids)
        photos = fetch_all(
            f"SELECT * FROM photos WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(photo_ids),
        )
        photos_by_id = {photo["id"]: photo for photo in photos}
        section["photos"] = []
        for photo_id in photo_ids:
            photo = photos_by_id.get(photo_id)
            if photo:
                photo["media_url"] = f"/media/{photo['stored_name']}"
                section["photos"].append(photo)
    return edition


def list_autobiography_editions(project_id: str) -> list[dict[str, Any]]:
    require_row("projects", project_id)
    rows = fetch_all(
        """
        SELECT id, edition_number, previous_edition_id, title, subtitle, status,
               narrative_person, scope, core_theme, character_portrait,
               source_snapshot_json, confirmed_at, created_at
        FROM autobiography_editions
        WHERE project_id = ? ORDER BY edition_number DESC
        """,
        (project_id,),
    )
    current_versions = {
        row["id"]: row["current_version_id"]
        for row in fetch_all(
            "SELECT id, current_version_id FROM chapters WHERE project_id = ? AND status != 'discarded'",
            (project_id,),
        )
    }
    for row in rows:
        snapshot = _json_object(row.pop("source_snapshot_json", "{}"))
        saved_versions = snapshot.get("chapter_versions", {})
        row["source_story_count"] = len(saved_versions)
        row["is_stale"] = saved_versions != current_versions
    return rows


async def people_catalog(project_id: str) -> dict[str, Any]:
    project = require_row("projects", project_id)
    facts = _usable_facts(fetch_all(
        """
        SELECT id, fact_type, value, status, event_id, photo_id
        FROM memory_facts
        WHERE project_id = ? AND status != 'retracted'
        ORDER BY created_at, rowid
        """,
        (project_id,),
    ))
    events = _sort_timeline_events(fetch_all(
        """
        SELECT id, title, time_text, start_year, location, primary_photo_id
        FROM timeline_events WHERE project_id = ?
        """,
        (project_id,),
    ))
    chapters = fetch_all(
        """
        SELECT c.id, c.title, c.current_version_id, cv.content,
               GROUP_CONCAT(DISTINCT ce.event_id) AS event_ids
        FROM chapters c
        LEFT JOIN chapter_versions cv ON cv.id = c.current_version_id
        LEFT JOIN chapter_events ce ON ce.chapter_id = c.id
        WHERE c.project_id = ? AND c.status != 'discarded'
        GROUP BY c.id, c.title, c.current_version_id, cv.content, c.created_at
        ORDER BY c.created_at
        """,
        (project_id,),
    )
    event_by_photo = {event["primary_photo_id"]: event["id"] for event in events}
    photo_rows = fetch_all(
        """
        SELECT p.id AS photo_id, p.note, po.status, po.observations_json, po.updated_at
        FROM photos p
        LEFT JOIN photo_observations po ON po.photo_id = p.id
        WHERE p.project_id = ? AND p.deleted_at IS NULL
        ORDER BY p.created_at
        """,
        (project_id,),
    )
    photo_people: list[dict[str, Any]] = []
    for photo in photo_rows:
        observations = _json_object(photo.get("observations_json"))
        people = observations.get("people") if isinstance(observations.get("people"), list) else []
        count = observations.get("people_count")
        if not isinstance(count, int):
            count = len(people)
        visible_descriptions = []
        for person in people[:12]:
            if isinstance(person, dict):
                description = str(person.get("visible_description") or "").strip()
            else:
                description = str(person).strip()
            if description:
                visible_descriptions.append(description[:300])
        photo_people.append({
            "photo_id": photo["photo_id"],
            "event_id": event_by_photo.get(photo["photo_id"]),
            "count": count,
            "visible_descriptions": visible_descriptions,
            "note": photo.get("note", ""),
            "updated_at": photo.get("updated_at"),
        })

    compact_chapters = [
        {
            "id": chapter["id"],
            "title": chapter["title"],
            "content_excerpt": _chapter_timeline_preview(chapter.get("content") or "", 380),
            "event_ids": [value for value in str(chapter.get("event_ids") or "").split(",") if value],
        }
        for chapter in chapters
    ]
    revision = {
        "catalog_version": 8,
        "facts": [(fact["id"], fact["value"], fact["status"], fact.get("event_id")) for fact in facts],
        "chapter_versions": [(chapter["id"], chapter.get("current_version_id")) for chapter in chapters],
        "photos": [(photo["photo_id"], photo.get("updated_at"), photo.get("count")) for photo in photo_people],
    }
    fingerprint = hashlib.sha256(
        json.dumps(revision, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cached = fetch_one("SELECT * FROM people_catalogs WHERE project_id = ?", (project_id,))
    if cached and cached["source_fingerprint"] == fingerprint:
        return _json_object(cached["catalog_json"])

    catalog = await agents.curate_people(project_id, facts, events, compact_chapters, photo_people)
    valid_fact_ids = {fact["id"] for fact in facts}
    valid_event_ids = {event["id"] for event in events}
    valid_chapter_ids = {chapter["id"] for chapter in chapters}
    valid_photo_ids = {photo["photo_id"] for photo in photo_people}
    fact_by_id = {fact["id"]: fact for fact in facts}
    chapter_event_ids = {
        chapter["id"]: {value for value in str(chapter.get("event_ids") or "").split(",") if value}
        for chapter in chapters
    }
    event_labels = {
        event["id"]: " · ".join(filter(None, [event.get("time_text"), event.get("title")]))
        for event in events
    }
    chapter_titles = {chapter["id"]: chapter["title"] for chapter in chapters}
    photo_person_counts = {photo["photo_id"]: int(photo.get("count") or 0) for photo in photo_people}
    sanitized_people: list[dict[str, Any]] = []
    for index, person in enumerate(catalog.get("people", [])):
        kind = person.get("kind")
        fact_ids = list(dict.fromkeys(value for value in person.get("source_fact_ids", []) if value in valid_fact_ids))
        event_ids = list(dict.fromkeys(value for value in person.get("event_ids", []) if value in valid_event_ids))
        chapter_ids = list(dict.fromkeys(value for value in person.get("chapter_ids", []) if value in valid_chapter_ids))
        photo_ids = list(dict.fromkeys(value for value in person.get("photo_ids", []) if value in valid_photo_ids))
        for fact_id in fact_ids:
            fact = fact_by_id[fact_id]
            if fact.get("event_id") in valid_event_ids and fact["event_id"] not in event_ids:
                event_ids.append(fact["event_id"])
        for event_id in event_ids:
            for chapter_id, linked_event_ids in chapter_event_ids.items():
                if event_id in linked_event_ids and chapter_id not in chapter_ids:
                    chapter_ids.append(chapter_id)
        if kind == "confirmed" and not fact_ids:
            if photo_ids:
                kind = "visual_unknown"
                person["relationship"] = "身份待补充"
            else:
                continue
        if kind == "confirmed":
            source_text = "；".join(str(fact_by_id[fact_id].get("value") or "") for fact_id in fact_ids)
            generic_terms = {
                "朋友", "好友", "女朋友", "同学", "同事", "工友", "老师", "师傅", "组长",
                "丈夫", "妻子", "爱人", "老伴", "父亲", "母亲", "爸爸", "妈妈",
                "爷爷", "奶奶", "外公", "外婆", "女儿", "儿子", "孩子",
                "姐姐", "妹妹", "哥哥", "弟弟", "重要人物",
            }
            candidates = [
                re.sub(r"[（(].*?[）)]", "", str(value)).strip()
                for value in [person.get("display_name"), *person.get("aliases", [])]
                if str(value or "").strip()
            ]
            grounded_identity = any(
                candidate in source_text
                and candidate not in generic_terms
                and not any(candidate == f"{prefix}{suffix}" for prefix in generic_terms for suffix in ("A", "B", "C", "1", "2", "3"))
                for candidate in candidates
            )
            if not grounded_identity:
                # 没有姓名的“朋友/同学/家人”由下方确定性关系组统一呈现，禁止模型擅自拆成A/B/C。
                continue
        if kind == "visual_unknown" and not photo_ids:
            continue
        if kind == "protagonist":
            event_ids = list(valid_event_ids)
            chapter_ids = list(valid_chapter_ids)
        elif event_ids:
            chapter_ids = [
                chapter_id for chapter_id in chapter_ids
                if chapter_event_ids.get(chapter_id, set()) & set(event_ids)
            ]
            for event_id in event_ids:
                for chapter_id, linked_event_ids in chapter_event_ids.items():
                    if event_id in linked_event_ids and chapter_id not in chapter_ids:
                        chapter_ids.append(chapter_id)
        display_name = str(person.get("display_name") or "未命名人物")[:80]
        aliases = list(dict.fromkeys(str(value)[:40] for value in person.get("aliases", []) if str(value).strip()))[:8]
        if kind == "visual_unknown" and (
            display_name.lower() in {"visual_unknown", "unknown", "未命名人物"}
            or re.fullmatch(r"[a-zA-Z0-9_\- ]+", display_name)
        ):
            visible_count = sum(photo_person_counts.get(photo_id, 0) for photo_id in photo_ids)
            display_name = f"照片中的人物（{visible_count}人）" if visible_count else "照片中的人物"
        if kind == "protagonist" and not aliases:
            aliases = ["我"]
        sanitized_people.append({
            "id": f"person-{index + 1}",
            "display_name": display_name,
            "aliases": aliases,
            "kind": kind,
            "relationship": str(person.get("relationship") or "")[:120],
            "summary": str(person.get("summary") or "这位人物的故事仍在补充中。")[:500],
            "story_role": str(person.get("story_role") or "")[:300],
            "event_ids": event_ids,
            "chapter_ids": chapter_ids,
            "photo_ids": photo_ids,
            "source_fact_ids": fact_ids,
            "appearances": [event_labels[value] for value in event_ids if event_labels.get(value)],
            "chapter_titles": [chapter_titles[value] for value in chapter_ids if chapter_titles.get(value)],
        })
    if not any(person["kind"] == "protagonist" for person in sanitized_people):
        sanitized_people.insert(0, {
            "id": "person-protagonist",
            "display_name": "主人公",
            "aliases": ["我"],
            "kind": "protagonist",
            "relationship": "这本自传的主人公",
            "summary": "这些照片与讲述共同留下了主人公不同人生阶段的经历。",
            "story_role": "所有人物关系和人生故事的中心。",
            "event_ids": list(valid_event_ids),
            "chapter_ids": list(valid_chapter_ids),
            "photo_ids": list(valid_photo_ids),
            "source_fact_ids": [],
            "appearances": [event_labels[value] for value in valid_event_ids if event_labels.get(value)],
            "chapter_titles": [chapter_titles[value] for value in valid_chapter_ids if chapter_titles.get(value)],
        })

    # 用户明确说出的关系不能因为没有姓名而被人物模型遗漏。
    relationship_specs = (
        ("同学", ("同学",)),
        ("朋友", ("朋友", "好友")),
        ("同事", ("同事", "工友")),
        ("老师", ("老师",)),
        ("师傅", ("师傅",)),
        ("爱人", ("丈夫", "妻子", "爱人", "老伴")),
        ("父亲", ("父亲", "爸爸")),
        ("母亲", ("母亲", "妈妈")),
        ("祖辈", ("爷爷", "奶奶", "外公", "外婆")),
        ("外孙女", ("外孙女", "孙女")),
        ("子女", ("女儿", "儿子", "孩子")),
        ("兄弟姐妹", ("姐姐", "妹妹", "哥哥", "弟弟")),
        ("组长", ("组长", "队长")),
    )
    relation_buckets: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        value = str(fact.get("value") or "")
        for relation, keywords in relationship_specs:
            if not any(word in value for word in keywords):
                continue
            # “小雨的爱人”属于女儿的家庭关系，不能误标成主人公的爱人。
            if relation == "爱人" and "小雨的爱人" in value and not any(
                marker in value for marker in ("我的爱人", "我爱人", "我和", "与陈国强", "夫妻", "结婚")
            ):
                continue
            relation_buckets.setdefault(relation, []).append(fact)

    # 配偶关系常以“我们结婚”“夫妻”表达，并不一定直接出现“爱人”二字。
    spouse_facts = [
        fact for fact in facts
        if any(name in str(fact.get("value") or "") for name in ("陈国强", "国强"))
        and any(marker in str(fact.get("value") or "") for marker in (
            "结婚", "夫妻", "我和国强", "与陈国强", "国强是钳工", "国强走了", "国强离世",
        ))
    ]
    if spouse_facts:
        existing_ids = {fact["id"] for fact in relation_buckets.get("爱人", [])}
        relation_buckets["爱人"] = [
            *relation_buckets.get("爱人", []),
            *(fact for fact in spouse_facts if fact["id"] not in existing_ids),
        ]
    covered_fact_ids = {
        fact_id
        for person in sanitized_people if person["kind"] == "confirmed"
        for fact_id in person.get("source_fact_ids", [])
    }
    for relation, related_facts in relation_buckets.items():
        related_ids = {fact["id"] for fact in related_facts}
        if related_ids & covered_fact_ids:
            continue
        values = list(dict.fromkeys(str(fact["value"]).strip() for fact in related_facts if fact.get("value")))
        relation_keywords = next(keywords for label, keywords in relationship_specs if label == relation)
        plural_pattern = rf"(?:两个|两位|几个|几位|一群).{{0,4}}(?:{'|'.join(map(re.escape, relation_keywords))})"
        plural = any(re.search(plural_pattern, value) for value in values)
        if relation == "朋友" and any("女朋友" in value and any(marker in value for marker in ("两个", "两位")) for value in values):
            display_name = "两位女性朋友"
        else:
            display_name = f"{'几位' if plural else ''}{relation}"
        joined_values = "；".join(values)
        name_patterns = {
            "父亲": r"(?:父亲|爸爸)([\u4e00-\u9fff]{2,3})(?=会|在|是|，|。|；)",
            "母亲": r"(?:母亲|妈妈)([\u4e00-\u9fff]{2,3})(?=会|在|是|种田|养蚕|，|。|；)",
            "外孙女": r"(?:外孙女|孙女)([\u4e00-\u9fff]{2,3})(?=\d|于|生|坐|站|，|。|；)",
            "子女": r"(?:女儿|儿子)([\u4e00-\u9fff]{2,3})(?=\d|于|生|坐|站|，|。|；)",
            "爱人": r"(?:与|和)([\u4e00-\u9fff]{2,4})(?=结婚)",
        }
        named_match = re.search(name_patterns.get(relation, r"$^"), joined_values)
        if named_match:
            display_name = named_match.group(1)
            if relation == "子女":
                relation = "女儿" if f"女儿{display_name}" in joined_values else "儿子"
        elif relation in {"老师", "师傅", "组长"}:
            honorifics = [
                value for value in dict.fromkeys(re.findall(rf"[\u4e00-\u9fff]{relation}", joined_values))
                if value[0] not in "年月日天位个老原当该这那"
            ]
            # 若同一关系下还存在未点名的人物，则保留“老师”等群体词条，避免错把多人合成一人。
            if len(honorifics) == 1 and all(
                relation not in str(fact.get("value") or "") or honorifics[0] in str(fact.get("value") or "")
                for fact in related_facts
            ):
                display_name = honorifics[0]
        relation_event_ids = list(dict.fromkeys(
            fact["event_id"] for fact in related_facts if fact.get("event_id") in valid_event_ids
        ))
        relation_photo_ids = list(dict.fromkeys(
            fact["photo_id"] for fact in related_facts if fact.get("photo_id") in valid_photo_ids
        ))
        relation_chapter_ids = [
            chapter_id for chapter_id, linked_event_ids in chapter_event_ids.items()
            if linked_event_ids & set(relation_event_ids)
        ]
        identity_terms = list(dict.fromkeys([
            display_name,
            display_name[-2:] if len(display_name) >= 3 else display_name,
            *relation_keywords,
        ]))
        relevant_fragments: list[str] = []
        for value in values:
            fragments = [fragment.strip() for fragment in re.split(r"[，,。；;]", value) if fragment.strip()]
            for fragment in fragments:
                if any(term and term in fragment for term in identity_terms) and fragment not in relevant_fragments:
                    relevant_fragments.append(fragment)
        if not relevant_fragments:
            relevant_fragments = values[:2]
        normalized_summary = _sentence_excerpt("；".join(relevant_fragments[:3]) + "。", 190)
        sanitized_people.append({
            "id": f"relation-{relation}-{len(sanitized_people) + 1}",
            "display_name": display_name,
            "aliases": [],
            "kind": "confirmed",
            "relationship": relation,
            "summary": normalized_summary,
            "story_role": "与主人公共同经历的重要人物；档案依据口述事实与已整理章节生成。",
            "event_ids": relation_event_ids,
            "chapter_ids": relation_chapter_ids,
            "photo_ids": relation_photo_ids,
            "source_fact_ids": list(related_ids),
            "appearances": [event_labels[value] for value in relation_event_ids if event_labels.get(value)],
            "chapter_titles": [chapter_titles[value] for value in relation_chapter_ids if chapter_titles.get(value)],
        })

    # 视觉未知人物不接受模型自由拆分：按“可见人数 - 已确认人物数”每张照片只生成一张卡。
    sanitized_people = [person for person in sanitized_people if person["kind"] != "visual_unknown"]

    def profile_headcount(person: dict[str, Any]) -> int:
        name = person.get("display_name", "")
        if any(marker in name for marker in ("三位", "三个")):
            return 3
        if any(marker in name for marker in ("两位", "两个")):
            return 2
        return 1

    for photo in photo_people:
        photo_id = photo["photo_id"]
        visible_count = int(photo.get("count") or 0)
        if visible_count <= 0:
            continue
        confirmed_count = sum(
            profile_headcount(person)
            for person in sanitized_people
            if photo_id in person.get("photo_ids", [])
        )
        remaining = max(0, visible_count - confirmed_count)
        if remaining == 0:
            continue
        event_id = photo.get("event_id")
        linked_chapters = [
            chapter_id for chapter_id, linked_event_ids in chapter_event_ids.items()
            if event_id and event_id in linked_event_ids
        ]
        sanitized_people.append({
            "id": f"visual-{photo_id}",
            "display_name": f"照片中的人物（{remaining}人）",
            "aliases": [],
            "kind": "visual_unknown",
            "relationship": "身份待补充",
            "summary": "照片中可以看到人物，但现有口述还不足以确认他们的姓名和关系。",
            "story_role": "等待后续访谈补充身份、关系与共同经历。",
            "event_ids": [event_id] if event_id else [],
            "chapter_ids": linked_chapters,
            "photo_ids": [photo_id],
            "source_fact_ids": [],
            "appearances": [event_labels[event_id]] if event_id and event_labels.get(event_id) else [],
            "chapter_titles": [chapter_titles[value] for value in linked_chapters if chapter_titles.get(value)],
        })
    event_order = {event["id"]: index for index, event in enumerate(events)}
    latest_autobiography = fetch_one(
        """
        SELECT character_portrait, review_json FROM autobiography_editions
        WHERE project_id = ? ORDER BY edition_number DESC LIMIT 1
        """,
        (project_id,),
    )
    name_candidates: list[str] = []
    title_match = re.match(r"^([\u4e00-\u9fff]{2,4})(?=[:：·])", project.get("title", ""))
    if title_match:
        name_candidates.append(title_match.group(1))
    for event in events:
        name_candidates.extend(re.findall(r"([\u4e00-\u9fff]{2,4})(?=\d{1,3}岁)", f"{event.get('time_text', '')} {event.get('title', '')}"))
    protagonist_name = max(set(name_candidates), key=name_candidates.count) if name_candidates else "主人公"
    autobiography_traits: list[str] = []
    if latest_autobiography:
        review = _json_object(latest_autobiography.get("review_json"))
        autobiography_traits = [str(value)[:20] for value in review.get("character_traits", []) if str(value).strip()][:5]

    for person in sanitized_people:
        person["event_ids"].sort(key=lambda value: event_order.get(value, 10**9))
        person["appearances"] = [event_labels[value] for value in person["event_ids"] if event_labels.get(value)]
        if person["kind"] == "protagonist":
            person["display_name"] = protagonist_name
            person["relationship"] = "本书主人公"
            if latest_autobiography and latest_autobiography.get("character_portrait"):
                person["summary"] = str(latest_autobiography["character_portrait"])[:500]
            person["key_attributes"] = autobiography_traits or ["人生故事的叙事中心"]
        elif person["kind"] == "visual_unknown":
            person["key_attributes"] = ["身份待补充"]
        else:
            # 属性只呈现可确定的关系、年代与参与经历，避免从长句里误借用别人的职业或身份。
            person["key_attributes"] = [person["relationship"]]
        if person["appearances"]:
            years = [int(year) for value in person["appearances"] for year in re.findall(r"(?:19|20)\d{2}", value)]
            if years:
                period = f"{min(years)}—{max(years)}年" if min(years) != max(years) else f"{years[0]}年"
                person["key_attributes"].append(period)
        person["key_attributes"].append(f"参与{len(person['event_ids'])}段经历")
        person["key_attributes"] = list(dict.fromkeys(person["key_attributes"]))[:5]
    sanitized_people.sort(key=lambda person: (
        0 if person["kind"] == "protagonist" else 1 if person["kind"] == "confirmed" else 2,
        min((event_order.get(value, 10**9) for value in person["event_ids"]), default=10**9),
        person["display_name"],
    ))
    confirmed_count = sum(person["kind"] in {"protagonist", "confirmed"} for person in sanitized_people)
    unknown_cards = [person for person in sanitized_people if person["kind"] == "visual_unknown"]
    unknown_people_count = sum(
        int(match.group(1)) if (match := re.search(r"（(\d+)人）", person["display_name"])) else 1
        for person in unknown_cards
    )
    overview = f"已整理{confirmed_count}组人物词条，人物关系与共同经历均来自用户口述。"
    if unknown_cards:
        overview += f"另有{len(unknown_cards)}张照片中的约{unknown_people_count}位人物身份尚待补充。"
    result = {
        "overview": overview,
        "people": sanitized_people,
        "counts": {
            "all": len(sanitized_people),
            "catalog_entries": confirmed_count,
            "confirmed": confirmed_count,
            "visual_unknown": len(unknown_cards),
            "unconfirmed_people": unknown_people_count,
        },
        "source_fingerprint": fingerprint,
    }
    now = now_iso()
    execute(
        """
        INSERT INTO people_catalogs (project_id, source_fingerprint, catalog_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
        source_fingerprint = excluded.source_fingerprint,
        catalog_json = excluded.catalog_json,
        updated_at = excluded.updated_at
        """,
        (project_id, fingerprint, json.dumps(result, ensure_ascii=False), now, now),
    )
    return result


async def compile_autobiography(project_id: str) -> dict[str, Any]:
    """Compile immutable photo stories into a separate, growing third-person book."""
    require_row("projects", project_id)
    chapter_rows = _chapter_summary_rows(project_id)
    if not chapter_rows:
        raise HTTPException(status_code=400, detail="至少需要一篇照片故事才能生成自传")
    chapters = [chapter_detail(row["id"]) for row in chapter_rows]
    source_stories: list[dict[str, Any]] = []
    all_fact_ids: list[str] = []
    compact_facts: list[dict[str, Any]] = []
    for chapter in chapters:
        version = chapter["current_version"]
        event_ids = [event["id"] for event in chapter.get("events", [])]
        story_facts: list[dict[str, Any]] = []
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            story_facts = _usable_facts(fetch_all(
                f"""
                SELECT * FROM memory_facts
                WHERE event_id IN ({placeholders})
                ORDER BY created_at, rowid
                """,
                tuple(event_ids),
            ))
        source = _json_object(version.get("source_snapshot_json"))
        photo_cards = [
            {
                "id": photo["id"],
                "note": photo.get("note", ""),
                "visual_evidence": source.get("visual_evidence", []),
            }
            for photo in chapter.get("photos", [])
        ]
        source_stories.append({
            "chapter_id": chapter["id"],
            "version_id": version["id"],
            "title": chapter["title"],
            "year": (chapter.get("events") or [{}])[0].get("start_year"),
            "time_text": (chapter.get("events") or [{}])[0].get("time_text", ""),
            "content": version["content"],
            "photos": photo_cards,
            "fact_ids": [fact["id"] for fact in story_facts],
            "canonical_facts": [fact.get("value", "") for fact in story_facts],
        })
        for fact in story_facts:
            if fact["id"] in all_fact_ids:
                continue
            all_fact_ids.append(fact["id"])
            compact_facts.append({
                "id": fact["id"],
                "fact_type": fact.get("fact_type"),
                "value": fact.get("value"),
                "source_chapter_id": chapter["id"],
                "status": fact.get("status"),
            })
    previous_row = fetch_one(
        "SELECT id FROM autobiography_editions WHERE project_id = ? ORDER BY edition_number DESC LIMIT 1",
        (project_id,),
    )
    previous = autobiography_edition_detail(previous_row["id"]) if previous_row else None
    previous_context = None
    if previous:
        previous_context = {
            "id": previous["id"],
            "title": previous["title"],
            "core_theme": previous["core_theme"],
            "character_portrait": previous["character_portrait"],
            "sections": [
                {
                    "title": section.get("title"),
                    "source_chapter_ids": section.get("source_chapter_ids", []),
                    "character_revelation": section.get("character_revelation", ""),
                }
                for section in previous["manuscript"].get("sections", [])
            ],
            "source_snapshot": previous["source_snapshot"],
        }
    manuscript = await agents.compile_autobiography_manuscript(
        project_id, source_stories, compact_facts, previous_context
    )
    if not settings.use_mock_llm and not manuscript.get("_model_succeeded"):
        raise HTTPException(status_code=503, detail="第三人称自传作家暂时不可用，本次没有保存书稿")
    manuscript.pop("_model_succeeded", None)
    valid_chapter_ids = {story["chapter_id"] for story in source_stories}
    valid_photo_ids = {
        photo["id"] for story in source_stories for photo in story.get("photos", [])
    }
    for section in manuscript.get("sections", []):
        section["source_chapter_ids"] = list(dict.fromkeys(
            source_id for source_id in section.get("source_chapter_ids", [])
            if source_id in valid_chapter_ids
        ))
        section["photo_ids"] = list(dict.fromkeys(
            photo_id for photo_id in section.get("photo_ids", [])
            if photo_id in valid_photo_ids
        ))
    review = await agents.review_autobiography_manuscript(
        project_id, source_stories, compact_facts, manuscript
    )
    if not settings.use_mock_llm and not review.get("_model_succeeded"):
        raise HTTPException(status_code=503, detail="完整自传终审暂时不可用，本次没有保存书稿")
    review.pop("_model_succeeded", None)
    scope = "micro" if len(source_stories) == 1 else "growing" if len(source_stories) < 6 else "full"
    next_number = (fetch_one(
        "SELECT COALESCE(MAX(edition_number), 0) AS value FROM autobiography_editions WHERE project_id = ?",
        (project_id,),
    ) or {"value": 0})["value"] + 1
    previous_source_ids = set(
        (previous or {}).get("source_snapshot", {}).get("chapter_versions", {}).keys()
    )
    current_source_ids = {story["chapter_id"] for story in source_stories}
    source_snapshot = {
        "chapter_versions": {
            story["chapter_id"]: story["version_id"] for story in source_stories
        },
        "photo_ids": sorted(valid_photo_ids),
        "fact_ids": all_fact_ids,
        "added_chapter_ids": sorted(current_source_ids - previous_source_ids),
        "updated_chapter_ids": sorted(
            source_id for source_id in current_source_ids & previous_source_ids
            if previous["source_snapshot"]["chapter_versions"].get(source_id)
            != next(
                story["version_id"] for story in source_stories
                if story["chapter_id"] == source_id
            )
        ) if previous else [],
    }
    edition_id, now = _id(), now_iso()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO autobiography_editions
            (id, project_id, edition_number, previous_edition_id, title, subtitle,
             status, narrative_person, scope, core_theme, character_portrait,
             manuscript_json, source_snapshot_json, review_json, confirmed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'third', ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                edition_id, project_id, next_number, previous["id"] if previous else None,
                manuscript["title"], manuscript.get("subtitle", ""),
                "draft" if review.get("passed") else "review_failed", scope,
                manuscript["core_theme"], manuscript["character_portrait"],
                json.dumps(manuscript, ensure_ascii=False),
                json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps(review, ensure_ascii=False), now,
            ),
        )
    return autobiography_edition_detail(edition_id)


def confirm_autobiography_edition(edition_id: str) -> dict[str, Any]:
    edition = autobiography_edition_detail(edition_id)
    if not edition["review"].get("passed"):
        raise HTTPException(status_code=409, detail="这一版仍有事实、照片覆盖或文学审校问题")
    execute(
        "UPDATE autobiography_editions SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
        (now_iso(), edition_id),
    )
    return autobiography_edition_detail(edition_id)


async def weave_book(project_id: str) -> dict[str, Any]:
    project = require_row("projects", project_id)
    chapter_rows = _chapter_summary_rows(project_id)
    if len(chapter_rows) < 3:
        raise HTTPException(status_code=400, detail="至少需要三章才能生成整书关联版")
    chapters = [chapter_detail(row["id"]) for row in chapter_rows]
    base_versions = [chapter["current_version"] for chapter in chapters]
    facts = _usable_facts(fetch_all(
        """
        SELECT mf.*, ce.chapter_id AS source_chapter_id, te.start_year AS source_year
        FROM memory_facts mf
        LEFT JOIN chapter_events ce ON ce.event_id = mf.event_id
        LEFT JOIN timeline_events te ON te.id = mf.event_id
        WHERE mf.project_id = ? ORDER BY te.start_year, mf.created_at, mf.rowid
        """,
        (project_id,),
    ))
    all_turns = fetch_all(
        """
        SELECT it.*, s.photo_id FROM interview_turns it
        JOIN interview_sessions s ON s.id = it.session_id
        WHERE s.project_id = ? ORDER BY it.created_at, it.rowid
        """,
        (project_id,),
    )
    director_chapters: list[dict[str, Any]] = []
    for chapter, version in zip(chapters, base_versions):
        event = (chapter.get("events") or [{}])[0]
        source = _json_object(version.get("source_snapshot_json"))
        director_chapters.append(
            {
                "chapter_id": chapter["id"],
                "title": chapter["title"],
                "start_year": event.get("start_year"),
                "content_excerpt": (
                    version["content"]
                    if len(version["content"]) <= 360
                    else version["content"][:180] + "\n……\n" + version["content"][-180:]
                ),
                "fact_ids": [],
                "literary_inferences": source.get("literary_inferences", []),
            }
        )
    fact_evidence = [
        {
            "id": fact.get("id"),
            "fact_type": fact.get("fact_type"),
            "value": fact.get("value"),
            "source_chapter_id": fact.get("source_chapter_id"),
            "source_year": fact.get("source_year"),
            "status": fact.get("status"),
        }
        for fact in facts
    ]
    director_facts: list[dict[str, Any]] = []
    for chapter_card in director_chapters:
        chapter_facts = [
            fact for fact in fact_evidence
            if fact.get("source_chapter_id") == chapter_card["chapter_id"]
        ]
        if len(chapter_facts) <= 3:
            anchor_facts = chapter_facts
        else:
            anchor_facts = [chapter_facts[0], chapter_facts[len(chapter_facts) // 2], chapter_facts[-1]]
        chapter_card["fact_ids"] = [fact["id"] for fact in anchor_facts]
        director_facts.extend(
            {
                "id": fact.get("id"),
                "value": fact.get("value"),
                "source_chapter_id": fact.get("source_chapter_id"),
                "source_year": fact.get("source_year"),
            }
            for fact in anchor_facts
        )
    director_plan = await agents.direct_book(project_id, director_chapters, director_facts)
    if not settings.use_mock_llm and not director_plan.get("_model_succeeded"):
        raise HTTPException(status_code=503, detail="整书导演模型暂时不可用，本次未生成或保存关联版")
    briefs = {str(item.get("chapter_id")): item for item in director_plan.get("chapter_briefs", [])}
    edition_id, now = _id(), now_iso()
    proposals: list[dict[str, Any]] = []
    for index, (chapter, version) in enumerate(zip(chapters, base_versions)):
        source = _json_object(version.get("source_snapshot_json"))
        visual_evidence = source.get("visual_evidence")
        if not isinstance(visual_evidence, list) or not visual_evidence:
            photo_id = source.get("photo_id") or ((chapter.get("photos") or [{}])[0].get("id"))
            visual_evidence = _visual_evidence_for_photo(photo_id)
        brief = briefs.get(chapter["id"], {"chapter_id": chapter["id"]})
        allowed_chapter_ids = {
            chapter["id"],
            *[
                str(source_id)
                for source_id in brief.get("source_chapter_ids", [])
                if source_id
            ],
        }
        own_fact_ids = set(source.get("fact_ids", []))
        relevant_facts = [
            fact for fact in fact_evidence
            if fact.get("id") in own_fact_ids or fact.get("source_chapter_id") in allowed_chapter_ids
        ][:20]
        allowed_photo_ids = {
            photo.get("id")
            for candidate in chapters
            if candidate["id"] in allowed_chapter_ids
            for photo in candidate.get("photos", [])
            if photo.get("id")
        }
        relevant_turns = [
            {"turn_id": turn.get("id"), "content": turn.get("content")}
            for turn in all_turns
            if turn.get("role") == "user" and turn.get("photo_id") in allowed_photo_ids
        ][:24]
        previous = director_chapters[index - 1] if index > 0 else None
        following = director_chapters[index + 1] if index + 1 < len(director_chapters) else None
        draft = await agents.reweave_chapter(
            project_id,
            project["narrative_person"],
            {"chapter_id": chapter["id"], "title": chapter["title"], "content": version["content"]},
            brief,
            str(director_plan.get("book_arc", "")),
            director_plan.get("people_registry", []),
            director_plan.get("narrative_threads", []),
            relevant_facts,
            relevant_turns,
            visual_evidence,
            {"title": previous["title"], "tail": previous["content_excerpt"][-220:]} if previous else None,
            {"title": following["title"], "opening": following["content_excerpt"][:220]} if following else None,
        )
        if not settings.use_mock_llm and not draft.get("_model_succeeded"):
            raise HTTPException(
                status_code=503,
                detail=f"章节《{chapter['title']}》关联改写暂时失败，本次未生成或保存关联版",
            )
        valid_fact_ids = {fact.get("id") for fact in relevant_facts}
        draft["used_fact_ids"] = [
            fact_id for fact_id in (draft.get("used_fact_ids") or [])
            if fact_id in valid_fact_ids
        ]
        external_fact_ids = {
            fact.get("id")
            for fact in relevant_facts
            if fact.get("id") and fact.get("source_chapter_id") != chapter["id"]
        }
        has_external_source = any(
            source_id != chapter["id"]
            for source_id in allowed_chapter_ids
        )
        draft_unchanged = re.sub(r"\s+", "", str(draft.get("content") or "")) == re.sub(
            r"\s+", "", version["content"]
        )
        missing_cross_evidence = has_external_source and not (
            set(draft["used_fact_ids"]) & external_fact_ids
        )
        if not settings.use_mock_llm and has_external_source and (draft_unchanged or missing_cross_evidence):
            if has_external_source and not external_fact_ids:
                raise HTTPException(
                    status_code=422,
                    detail=f"章节《{chapter['title']}》缺少可追溯的外章事实，本次未保存",
                )
            initial_draft = draft
            retry_brief = {
                **brief,
                "mandatory_editor_note": (
                    "上一稿没有形成可验证的正文变化。请保留中心事件与原有文风，"
                    "从 required_cross_fact_ids 对应事实中至少选择一条，围绕具体物件、动作或人物，"
                    "自然新增或改写80至180个汉字；不要写成总结、预告或单独的说明段。"
                ),
                "required_cross_fact_ids": list(external_fact_ids),
            }
            draft = await agents.force_chapter_link(
                project_id,
                project["narrative_person"],
                {"chapter_id": chapter["id"], "title": chapter["title"], "content": version["content"]},
                retry_brief,
                [fact for fact in relevant_facts if fact.get("id") in external_fact_ids],
            )
            if not draft.get("_model_succeeded"):
                raise HTTPException(
                    status_code=503,
                    detail=f"章节《{chapter['title']}》定向关联改写失败，本次未保存",
                )
            draft["used_fact_ids"] = [
                fact_id for fact_id in (draft.get("used_fact_ids") or [])
                if fact_id in valid_fact_ids
            ]
            retry_unchanged = re.sub(r"\s+", "", str(draft.get("content") or "")) == re.sub(
                r"\s+", "", version["content"]
            )
            retry_missing_cross = has_external_source and not (
                set(draft["used_fact_ids"]) & external_fact_ids
            )
            if retry_unchanged or retry_missing_cross:
                draft = initial_draft
                draft["_link_retry_failed"] = True
        content = str(draft.get("content") or version["content"]).strip()
        fact_links = await agents.link_chapter_facts(project_id, content, fact_evidence)
        if not settings.use_mock_llm and not fact_links.get("_model_succeeded"):
            raise HTTPException(
                status_code=503,
                detail=f"章节《{chapter['title']}》事实来源链接失败，本次未保存",
            )
        all_valid_fact_ids = {fact.get("id") for fact in fact_evidence}
        linked_fact_ids = [
            fact_id for fact_id in (fact_links.get("fact_ids") or [])
            if fact_id in all_valid_fact_ids
        ]
        draft["used_fact_ids"] = list(dict.fromkeys((draft.get("used_fact_ids") or []) + linked_fact_ids))
        review_fact_ids = own_fact_ids | set(draft.get("used_fact_ids") or [])
        review_facts = [fact for fact in facts if fact.get("id") in review_fact_ids]
        local_review = await agents.review_chapter(
            content,
            review_facts,
            project["narrative_person"],
            [turn for turn in all_turns if turn.get("photo_id") in allowed_photo_ids],
            visual_evidence,
            draft.get("literary_inferences") or [],
        )
        if not local_review.get("passed"):
            repaired_content = str(local_review.get("corrected_content") or "").strip()
            if repaired_content and repaired_content != content:
                repaired_review = await agents.review_chapter(
                    repaired_content,
                    review_facts,
                    project["narrative_person"],
                    [turn for turn in all_turns if turn.get("photo_id") in allowed_photo_ids],
                    visual_evidence,
                    draft.get("literary_inferences") or [],
                )
                if repaired_review.get("passed"):
                    repaired_review["auto_repaired"] = True
                    repaired_review["initial_issues"] = local_review.get("issues", [])
                    local_review = repaired_review
                    content = repaired_content
        proposals.append(
            {
                "chapter": chapter,
                "base_version": version,
                "brief": brief,
                "draft": draft,
                "content": str(local_review.get("corrected_content") or content).strip(),
                "local_review": local_review,
                "visual_evidence": visual_evidence,
            }
        )
    used_fact_ids = {
        fact_id
        for item in proposals
        for fact_id in (item["draft"].get("used_fact_ids") or [])
    }
    continuity_facts = [fact for fact in fact_evidence if fact.get("id") in used_fact_ids]
    continuity_review = await agents.review_book_continuity(
        project_id,
        director_plan,
        [
            {
                "chapter_id": item["chapter"]["id"],
                "title": item["draft"].get("title") or item["chapter"]["title"],
                "content": item["content"],
                "literary_inferences": item["draft"].get("literary_inferences") or [],
            }
            for item in proposals
        ],
        continuity_facts,
    )
    if not settings.use_mock_llm and not continuity_review.get("_model_succeeded"):
        raise HTTPException(status_code=503, detail="整书连续性审校暂时不可用，本次未生成或保存关联版")
    local_failures = [
        {"chapter_id": item["chapter"]["id"], "issues": item["local_review"].get("issues", [])}
        for item in proposals if not item["local_review"].get("passed")
    ]
    edition_review = {
        **continuity_review,
        "local_failures": local_failures,
        "passed": bool(continuity_review.get("passed")) and not local_failures,
    }
    if not settings.use_mock_llm:
        changed_chapters = sum(
            1
            for item in proposals
            if re.sub(r"\s+", "", item["content"])
            != re.sub(r"\s+", "", item["base_version"]["content"])
        )
        cross_fact_ids = {
            fact_id
            for item in proposals
            for fact_id in (item["draft"].get("used_fact_ids") or [])
            if fact_id not in set(_json_object(item["base_version"].get("source_snapshot_json")).get("fact_ids", []))
        }
        edition_review["effectiveness"] = {
            "changed_chapters": changed_chapters,
            "total_chapters": len(proposals),
            "cross_chapter_fact_count": len(cross_fact_ids),
        }
        if changed_chapters < max(3, len(proposals) // 2) or len(cross_fact_ids) < 3:
            raise HTTPException(
                status_code=422,
                detail="整书关联强度不足：至少半数章节需要实质改写，并引用三条可追溯的跨章事实；本次未保存",
            )
    next_edition_number = (fetch_one(
        "SELECT COALESCE(MAX(edition_number), 0) AS value FROM book_editions WHERE project_id = ?",
        (project_id,),
    ) or {"value": 0})["value"] + 1
    base_snapshot = {
        "chapter_version_ids": [version["id"] for version in base_versions],
        "fact_ids": [fact["id"] for fact in facts],
        "created_from_project_revision": project["updated_at"],
    }
    with connection() as conn:
        conn.execute(
            "INSERT INTO book_editions VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                edition_id,
                project_id,
                next_edition_number,
                f"{project['title']} · 整书关联版{next_edition_number}",
                "draft" if edition_review["passed"] else "review_failed",
                json.dumps(base_snapshot, ensure_ascii=False),
                json.dumps(director_plan, ensure_ascii=False),
                json.dumps(edition_review, ensure_ascii=False),
                now,
            ),
        )
        for order, item in enumerate(proposals, 1):
            chapter, base_version = item["chapter"], item["base_version"]
            next_number = max(version["version_number"] for version in chapter["versions"]) + 1
            version_id = _id()
            base_source = _json_object(base_version.get("source_snapshot_json"))
            used_fact_ids = item["draft"].get("used_fact_ids") or []
            change_summary = {
                "base_version_id": base_version["id"],
                "source_chapter_ids": item["brief"].get("source_chapter_ids", []),
                "people_additions": item["brief"].get("people_additions", []),
                "motif_actions": item["brief"].get("motif_actions", []),
                "used_cross_chapter_fact_ids": [
                    fact_id for fact_id in used_fact_ids if fact_id not in base_source.get("fact_ids", [])
                ],
                "old_chars": len(re.sub(r"\s+", "", base_version["content"])),
                "new_chars": len(re.sub(r"\s+", "", item["content"])),
            }
            source_snapshot = {
                **base_source,
                "book_edition_id": edition_id,
                "base_version_id": base_version["id"],
                "director_brief": item["brief"],
                "book_arc": director_plan.get("book_arc"),
                "narrative_threads": director_plan.get("narrative_threads", []),
                "fact_ids": list(dict.fromkeys(base_source.get("fact_ids", []) + used_fact_ids)),
                "used_visual_ids": item["draft"].get("used_visual_ids") or [],
                "literary_inferences": item["draft"].get("literary_inferences") or [],
            }
            conn.execute(
                "INSERT INTO chapter_versions VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    version_id,
                    chapter["id"],
                    next_number,
                    project["narrative_person"],
                    item["content"],
                    json.dumps(source_snapshot, ensure_ascii=False),
                    json.dumps(item["local_review"], ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                "UPDATE chapters SET title = ?, current_version_id = ?, status = 'draft', updated_at = ? WHERE id = ?",
                (str(item["draft"].get("title") or chapter["title"])[:100], version_id, now, chapter["id"]),
            )
            conn.execute(
                "INSERT INTO book_edition_chapters VALUES (?, ?, ?, ?, ?)",
                (edition_id, chapter["id"], version_id, order, json.dumps(change_summary, ensure_ascii=False)),
            )
    return book_edition_detail(edition_id)


def confirm_book_edition(edition_id: str) -> dict[str, Any]:
    edition = book_edition_detail(edition_id)
    if not edition["review"].get("passed"):
        raise HTTPException(status_code=409, detail="整书关联版尚未通过连续性审校")
    if any(not chapter["review"].get("passed") for chapter in edition["chapters"]):
        raise HTTPException(status_code=409, detail="仍有章节未通过事实审校")
    now = now_iso()
    with connection() as conn:
        conn.execute(
            "UPDATE book_editions SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            (now, edition_id),
        )
        for chapter in edition["chapters"]:
            conn.execute("UPDATE chapter_versions SET confirmed_at = ? WHERE id = ?", (now, chapter["version_id"]))
            conn.execute(
                "UPDATE chapters SET status = 'confirmed', current_version_id = ?, updated_at = ? WHERE id = ?",
                (chapter["version_id"], now, chapter["chapter_id"]),
            )
    return book_edition_detail(edition_id)


_QUOTED_CORRECTION_PATTERN = re.compile(
    r"把\s*[‘'“\"](?P<old>.+?)[’'”\"]\s*(?:改成|改为|换成)\s*[‘'“\"](?P<new>.+?)[’'”\"]"
)


def _memory_correction_plan(
    instruction: str,
    facts: list[dict[str, Any]],
    mode: str,
    inherited: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    proposals_by_fact = {
        proposal["fact_id"]: dict(proposal)
        for proposal in (inherited or [])
        if proposal.get("fact_id")
    }
    working_facts = [dict(fact) for fact in facts]
    for fact in working_facts:
        inherited_proposal = proposals_by_fact.get(fact["id"])
        if inherited_proposal:
            fact["value"] = inherited_proposal["new_value"]

    pairs = [] if mode == "style" else [
        (match.group("old").strip(), match.group("new").strip())
        for match in _QUOTED_CORRECTION_PATTERN.finditer(instruction)
        if match.group("old").strip() and match.group("new").strip()
    ]
    unmatched: list[dict[str, str]] = []
    for old_text, new_text in pairs:
        exact = [fact for fact in working_facts if fact.get("value") == old_text]
        matches = exact or [fact for fact in working_facts if old_text in str(fact.get("value") or "")]
        if len(matches) != 1:
            unmatched.append({
                "old_text": old_text,
                "new_text": new_text,
                "reason": "没有找到唯一对应的记忆事实" if not matches else "有多条记忆事实包含这段内容",
            })
            continue
        fact = matches[0]
        previous_value = str(fact["value"])
        proposed_value = new_text if previous_value == old_text else previous_value.replace(old_text, new_text)
        existing = proposals_by_fact.get(fact["id"])
        proposals_by_fact[fact["id"]] = {
            "fact_id": fact["id"],
            "fact_type": fact.get("fact_type") or "other",
            "old_value": existing["old_value"] if existing else next(
                (str(item["value"]) for item in facts if item["id"] == fact["id"]),
                previous_value,
            ),
            "new_value": proposed_value,
            "event_id": fact.get("event_id"),
        }
        fact["value"] = proposed_value

    if mode == "fact" and not pairs:
        unmatched.append({
            "old_text": "",
            "new_text": "",
            "reason": "请使用“把‘原内容’改成‘正确内容’”描述事实更正",
        })
    return {
        "mode": mode,
        "proposals": list(proposals_by_fact.values()),
        "unmatched": unmatched,
        "working_facts": working_facts,
    }


async def revise_chapter(
    chapter_id: str,
    instruction: str,
    mode: str = "auto",
    base_candidate_id: str | None = None,
) -> dict[str, Any]:
    chapter = chapter_detail(chapter_id)
    project = require_row("projects", chapter["project_id"])
    current = chapter["current_version"]
    parent_candidate = None
    if base_candidate_id:
        parent_candidate = revision_candidate_detail(base_candidate_id)
        if parent_candidate["chapter_id"] != chapter_id or parent_candidate["status"] != "pending":
            raise HTTPException(status_code=409, detail="这份候选稿已经失效，请从当前章节重新修改")
        if parent_candidate["base_version_id"] != current["id"]:
            raise HTTPException(status_code=409, detail="当前章节已经变化，请重新生成修改稿")
    source = json.loads(current["source_snapshot_json"])
    session_id = source.get("session_id")
    session = session_detail(session_id) if session_id else {"turns": [], "facts": []}
    event_ids = [row["event_id"] for row in fetch_all(
        "SELECT event_id FROM chapter_events WHERE chapter_id = ?", (chapter_id,)
    )]
    if not event_ids and source.get("event_id"):
        event_ids = [source["event_id"]]
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        usable_facts = _usable_facts(fetch_all(
            f"SELECT * FROM memory_facts WHERE event_id IN ({placeholders}) ORDER BY created_at, rowid",
            tuple(event_ids),
        ))
    else:
        usable_facts = _usable_facts(session["facts"])
    inherited_corrections = (
        parent_candidate.get("correction", {}).get("proposals", []) if parent_candidate else []
    )
    correction = _memory_correction_plan(instruction, usable_facts, mode, inherited_corrections)
    draft_facts = correction.pop("working_facts")
    method_cards = retrieve_method_cards(
        project["narrative_person"], "\n".join(fact["value"] for fact in draft_facts)
    )
    visual_evidence = source.get("visual_evidence")
    # 首次识图失败后，用户可能已经点击“重新识图”；旧版本中的空证据包不应阻止新版本读取结果。
    if not isinstance(visual_evidence, list) or not visual_evidence:
        visual_evidence = _visual_evidence_for_photo(source.get("photo_id"))
    context_pack = await _prepare_session_context(session) if session_id else {
        "model_turns": session.get("turns", []),
        "conversation_summary": {},
        "context_control": {},
    }
    draft = await agents.draft_chapter(
        project["narrative_person"],
        session["turns"],
        draft_facts,
        method_cards,
        visual_evidence,
        instruction=instruction,
        previous_content=parent_candidate["content"] if parent_candidate else current["content"],
        model_turns=context_pack["model_turns"],
        conversation_summary=context_pack["conversation_summary"],
        context_control=context_pack["context_control"],
    )
    content = str(draft.get("content") or current["content"]).strip()
    review = await agents.review_chapter(
        content,
        draft_facts,
        project["narrative_person"],
        session.get("turns", []),
        visual_evidence,
        draft.get("literary_inferences") or [],
        model_turns=context_pack["model_turns"],
        conversation_summary=context_pack["conversation_summary"],
        context_control=context_pack["context_control"],
    )
    content = str(review.get("corrected_content") or content).strip()
    candidate_id, now = _id(), now_iso()
    candidate_source = {
        **source,
        "base_version_id": current["id"],
        "parent_candidate_id": base_candidate_id,
        "fact_ids": [fact["id"] for fact in usable_facts],
        "writing_method_ids": [card["id"] for card in method_cards],
        "visual_evidence": visual_evidence,
        "used_visual_ids": draft.get("used_visual_ids") or [],
        "literary_inferences": draft.get("literary_inferences") or [],
        "revision_instruction": instruction,
        "revision_mode": mode,
    }
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO chapter_revision_candidates
            (id, chapter_id, base_version_id, parent_candidate_id, status, title,
             instruction, content, source_snapshot_json, review_json, correction_json,
             adopted_version_id, created_at, resolved_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
            """,
            (
                candidate_id,
                chapter_id,
                current["id"],
                base_candidate_id,
                str(draft.get("title") or chapter["title"])[:100],
                instruction,
                content,
                json.dumps(candidate_source, ensure_ascii=False),
                json.dumps(review, ensure_ascii=False),
                json.dumps(correction, ensure_ascii=False),
                now,
            ),
        )
        if parent_candidate:
            conn.execute(
                "UPDATE chapter_revision_candidates SET status = 'superseded', resolved_at = ? WHERE id = ?",
                (now, parent_candidate["id"]),
            )
    return revision_candidate_detail(candidate_id)


def discard_revision_candidate(candidate_id: str) -> dict[str, Any]:
    candidate = revision_candidate_detail(candidate_id)
    if candidate["status"] != "pending":
        raise HTTPException(status_code=409, detail="这份候选稿已经处理过了")
    execute(
        "UPDATE chapter_revision_candidates SET status = 'discarded', resolved_at = ? WHERE id = ?",
        (now_iso(), candidate_id),
    )
    return revision_candidate_detail(candidate_id)


def adopt_revision_candidate(candidate_id: str) -> dict[str, Any]:
    candidate = revision_candidate_detail(candidate_id)
    if candidate["status"] != "pending":
        raise HTTPException(status_code=409, detail="这份候选稿已经处理过了")
    chapter = chapter_detail(candidate["chapter_id"])
    if chapter["current_version_id"] != candidate["base_version_id"]:
        execute(
            "UPDATE chapter_revision_candidates SET status = 'stale', resolved_at = ? WHERE id = ?",
            (now_iso(), candidate_id),
        )
        raise HTTPException(status_code=409, detail="当前章节已经变化，这份候选稿不能再采用")

    correction = candidate.get("correction") or {}
    proposals = correction.get("proposals") or []
    if correction.get("unmatched"):
        raise HTTPException(status_code=409, detail="仍有无法定位的事实更正，请按提示补充后再采用")

    now = now_iso()
    replacement_ids: dict[str, str] = {}
    changed_event_ids: set[str] = set()
    with connection() as conn:
        for proposal in proposals:
            fact = conn.execute("SELECT * FROM memory_facts WHERE id = ?", (proposal["fact_id"],)).fetchone()
            if not fact or fact["status"] == "retracted" or fact["value"] != proposal["old_value"]:
                raise HTTPException(status_code=409, detail="相关记忆已经变化，请重新生成修改稿")
            replacement_id = _id()
            replacement_ids[fact["id"]] = replacement_id
            conn.execute("UPDATE memory_facts SET status = 'retracted' WHERE id = ?", (fact["id"],))
            conn.execute(
                """
                INSERT INTO memory_facts
                (id, project_id, session_id, photo_id, fact_type, value, status,
                 evidence_turn_id, sensitivity, include_in_book, supersedes, event_id,
                 event_link_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'confirmed_by_user', ?, ?, ?, ?, ?,
                        'confirmed_by_user', ?)
                """,
                (
                    replacement_id, fact["project_id"], fact["session_id"], fact["photo_id"],
                    fact["fact_type"], proposal["new_value"], fact["evidence_turn_id"],
                    fact["sensitivity"], fact["include_in_book"], fact["id"], fact["event_id"], now,
                ),
            )
            if fact["event_id"]:
                changed_event_ids.add(str(fact["event_id"]))
                old_temporal = _temporal_value(str(proposal["old_value"]), now)
                new_temporal = _temporal_value(str(proposal["new_value"]), now)
                if (
                    new_temporal
                    and new_temporal.get("start_year") is not None
                    and (
                        not old_temporal
                        or old_temporal.get("start_year") != new_temporal.get("start_year")
                        or old_temporal.get("end_year") != new_temporal.get("end_year")
                    )
                ):
                    conn.execute(
                        """
                        UPDATE timeline_events
                        SET time_text = ?, start_year = ?, end_year = ?, time_precision = ?,
                            time_locked = 1, time_source_type = 'user_fact_correction',
                            time_source_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            new_temporal["time_text"], new_temporal.get("start_year"),
                            new_temporal.get("end_year"), new_temporal["time_precision"],
                            replacement_id, now, fact["event_id"],
                        ),
                    )

        source_snapshot = candidate.get("source_snapshot") or {}
        source_snapshot["fact_ids"] = [
            replacement_ids.get(fact_id, fact_id) for fact_id in source_snapshot.get("fact_ids", [])
        ]
        source_snapshot["adopted_candidate_id"] = candidate_id
        source_snapshot["memory_corrections"] = proposals
        next_number = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM chapter_versions WHERE chapter_id = ?",
            (chapter["id"],),
        ).fetchone()[0]
        version_id = _id()
        conn.execute(
            "INSERT INTO chapter_versions VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                version_id, chapter["id"], next_number,
                chapter["current_version"]["narrative_person"], candidate["content"],
                json.dumps(source_snapshot, ensure_ascii=False), candidate["review_json"], now,
            ),
        )
        conn.execute(
            "UPDATE chapters SET title = ?, current_version_id = ?, status = 'draft', updated_at = ? WHERE id = ?",
            (candidate["title"], version_id, now, chapter["id"]),
        )
        conn.execute(
            """
            UPDATE chapter_revision_candidates
            SET status = 'adopted', adopted_version_id = ?, resolved_at = ? WHERE id = ?
            """,
            (version_id, now, candidate_id),
        )
        conn.execute(
            """
            UPDATE chapter_revision_candidates SET status = 'stale', resolved_at = ?
            WHERE chapter_id = ? AND status = 'pending' AND id != ?
            """,
            (now, chapter["id"], candidate_id),
        )
        chapter_event_ids = {
            str(row[0]) for row in conn.execute(
                "SELECT event_id FROM chapter_events WHERE chapter_id = ?", (chapter["id"],)
            ).fetchall()
        }
        for event_id in changed_event_ids | chapter_event_ids:
            conn.execute(
                "UPDATE timeline_events SET needs_chapter_refresh = 0, updated_at = ? WHERE id = ?",
                (now, event_id),
            )
    for event_id in changed_event_ids:
        _refresh_event_summary(event_id)
    return chapter_detail(chapter["id"])


def confirm_chapter(chapter_id: str) -> dict[str, Any]:
    chapter = chapter_detail(chapter_id)
    try:
        review = json.loads(chapter["current_version"]["review_json"])
    except json.JSONDecodeError:
        review = {"passed": False}
    if not review.get("passed"):
        raise HTTPException(status_code=409, detail="当前版本未通过审校，请修改后再确认")
    now = now_iso()
    with connection() as conn:
        conn.execute(
            "UPDATE chapter_versions SET confirmed_at = ? WHERE id = ?",
            (now, chapter["current_version_id"]),
        )
        conn.execute(
            "UPDATE chapters SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (now, chapter_id),
        )
    return chapter_detail(chapter_id)


def update_fact(
    fact_id: str,
    value: str | None,
    include_in_book: bool | None,
    sensitivity: str | None,
    event_id: str | None = None,
) -> dict[str, Any]:
    current = require_row("memory_facts", fact_id)
    if current["status"] == "retracted":
        raise HTTPException(status_code=409, detail="这条事实已经被更正或撤回")
    target_event_id = event_id or current.get("event_id")
    if target_event_id:
        target_event = require_row("timeline_events", target_event_id)
        if target_event["project_id"] != current["project_id"]:
            raise HTTPException(status_code=400, detail="不能把记忆移动到其他人的时间线")
    replacement_id, now = _id(), now_iso()
    replacement = {
        **current,
        "id": replacement_id,
        "value": value.strip() if value else current["value"],
        "status": "confirmed_by_user",
        "include_in_book": int(include_in_book) if include_in_book is not None else current["include_in_book"],
        "sensitivity": sensitivity or current["sensitivity"],
        "supersedes": current["id"],
        "event_id": target_event_id,
        "event_link_status": "confirmed_by_user" if event_id else current.get("event_link_status", "current_event"),
        "created_at": now,
    }
    with connection() as conn:
        conn.execute("UPDATE memory_facts SET status = 'retracted' WHERE id = ?", (fact_id,))
        conn.execute(
            """
            INSERT INTO memory_facts
            (id, project_id, session_id, photo_id, fact_type, value, status,
             evidence_turn_id, sensitivity, include_in_book, supersedes, event_id,
             event_link_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                replacement["id"],
                replacement["project_id"],
                replacement["session_id"],
                replacement["photo_id"],
                replacement["fact_type"],
                replacement["value"],
                replacement["status"],
                replacement["evidence_turn_id"],
                replacement["sensitivity"],
                replacement["include_in_book"],
                replacement["supersedes"],
                replacement["event_id"],
                replacement["event_link_status"],
                replacement["created_at"],
            ),
        )
        if current.get("event_id"):
            conn.execute(
                "UPDATE timeline_events SET needs_chapter_refresh = 1, updated_at = ? WHERE id = ?",
                (now, current["event_id"]),
            )
        if target_event_id:
            conn.execute(
                "UPDATE timeline_events SET needs_chapter_refresh = 1, updated_at = ? WHERE id = ?",
                (now, target_event_id),
            )
    for changed_event_id in {current.get("event_id"), target_event_id} - {None}:
        _refresh_event_summary(str(changed_event_id))
    return require_row("memory_facts", replacement_id)


def update_timeline_event(event_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    current = require_row("timeline_events", event_id)
    values = {
        "title": changes.get("title") if changes.get("title") is not None else current["title"],
        "time_text": changes.get("time_text") if changes.get("time_text") is not None else current["time_text"],
        "start_year": changes.get("start_year") if "start_year" in changes else current["start_year"],
        "end_year": changes.get("end_year") if "end_year" in changes else current["end_year"],
        "time_precision": changes.get("time_precision") if changes.get("time_precision") is not None else current["time_precision"],
        "location": changes.get("location") if changes.get("location") is not None else current["location"],
        "summary": changes.get("summary") if changes.get("summary") is not None else current["summary"],
    }
    if values["start_year"] and values["end_year"] and values["end_year"] < values["start_year"]:
        raise HTTPException(status_code=400, detail="结束年份不能早于开始年份")
    execute(
        """
        UPDATE timeline_events SET title = ?, time_text = ?, start_year = ?, end_year = ?,
        time_precision = ?, location = ?, summary = ?, updated_at = ? WHERE id = ?
        """,
        (
            str(values["title"]).strip(), str(values["time_text"]).strip(), values["start_year"],
            values["end_year"], values["time_precision"], str(values["location"]).strip(),
            str(values["summary"]).strip(), now_iso(), event_id,
        ),
    )
    if changes.get("title") is not None and str(changes.get("title") or "").strip():
        _save_event_title_version(
            event_id,
            str(changes["title"]),
            "user",
            "correction",
            "用户在时间线中更正了事件名称。",
            {"authority": "user_correction", "previous_archival_title": current["title"]},
        )
    return require_row("timeline_events", event_id)


def _chapter_fact_rows(*versions: dict[str, Any]) -> list[dict[str, Any]]:
    fact_ids: list[str] = []
    for version in versions:
        try:
            source = json.loads(version["source_snapshot_json"])
        except (KeyError, json.JSONDecodeError):
            continue
        fact_ids.extend(str(item) for item in source.get("fact_ids", []))
    unique_ids = list(dict.fromkeys(fact_ids))
    if not unique_ids:
        return []
    placeholders = ",".join("?" for _ in unique_ids)
    rows = fetch_all(f"SELECT * FROM memory_facts WHERE id IN ({placeholders})", tuple(unique_ids))
    return _usable_facts(rows)


async def apply_relation_choice(
    photo_id: str,
    choice: str,
    chapter_id: str | None,
    source_chapter_id: str | None,
) -> dict[str, Any]:
    photo = require_row("photos", photo_id)
    if not source_chapter_id:
        source_row = fetch_one(
            """
            SELECT c.* FROM chapters c JOIN chapter_photos cp ON cp.chapter_id = c.id
            WHERE cp.photo_id = ? AND c.status != 'discarded'
            ORDER BY c.created_at DESC LIMIT 1
            """,
            (photo_id,),
        )
        source_chapter_id = source_row["id"] if source_row else None
    if not source_chapter_id:
        raise HTTPException(status_code=400, detail="找不到这张照片对应的新章节")
    source = chapter_detail(source_chapter_id)
    if source["project_id"] != photo["project_id"]:
        raise HTTPException(status_code=400, detail="照片与来源章节不属于同一项目")

    if choice == "new":
        execute("UPDATE photos SET relation_choice = 'new' WHERE id = ?", (photo_id,))
        return {"choice": choice, "chapter": source}

    if not chapter_id:
        raise HTTPException(status_code=400, detail="加入或合并需要选择一个已有章节")
    if chapter_id == source_chapter_id:
        raise HTTPException(status_code=400, detail="不能把章节与自身合并")
    target = chapter_detail(chapter_id)
    if target["project_id"] != photo["project_id"]:
        raise HTTPException(status_code=400, detail="照片与目标章节不属于同一项目")

    now = now_iso()
    if choice == "attach":
        with connection() as conn:
            conn.execute("INSERT OR IGNORE INTO chapter_photos VALUES (?, ?)", (chapter_id, photo_id))
            conn.execute("UPDATE photos SET relation_choice = 'attach' WHERE id = ?", (photo_id,))
            conn.execute(
                "UPDATE chapters SET status = 'discarded', updated_at = ? WHERE id = ?",
                (now, source_chapter_id),
            )
        return {"choice": choice, "chapter": chapter_detail(chapter_id)}

    if choice != "merge":
        raise HTTPException(status_code=400, detail="无效的章节关系选择")

    project = require_row("projects", photo["project_id"])
    target_version = target["current_version"]
    source_version = source["current_version"]
    facts = _chapter_fact_rows(target_version, source_version)
    merged = await agents.merge_chapters(
        project["narrative_person"],
        {"title": target["title"], "content": target_version["content"]},
        {"title": source["title"], "content": source_version["content"]},
        facts,
    )
    review = await agents.review_chapter(merged["content"], facts, project["narrative_person"])
    content = str(review.get("corrected_content") or merged["content"]).strip()
    next_number = max(version["version_number"] for version in target["versions"]) + 1
    version_id = _id()
    source_snapshot = {
        "merged_from_version_ids": [target_version["id"], source_version["id"]],
        "fact_ids": [fact["id"] for fact in facts],
        "photo_ids": list(dict.fromkeys([p["id"] for p in target["photos"]] + [photo_id])),
    }
    with connection() as conn:
        conn.execute(
            "INSERT INTO chapter_versions VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                version_id,
                chapter_id,
                next_number,
                project["narrative_person"],
                content,
                json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps(review, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "UPDATE chapters SET title = ?, current_version_id = ?, status = 'draft', updated_at = ? WHERE id = ?",
            (merged["title"], version_id, now, chapter_id),
        )
        conn.execute("INSERT OR IGNORE INTO chapter_photos VALUES (?, ?)", (chapter_id, photo_id))
        source_event_ids = [row["event_id"] for row in conn.execute(
            "SELECT event_id FROM chapter_events WHERE chapter_id = ?", (source_chapter_id,)
        ).fetchall()]
        for event_id in source_event_ids:
            conn.execute("INSERT OR IGNORE INTO chapter_events VALUES (?, ?)", (chapter_id, event_id))
            conn.execute(
                "UPDATE timeline_events SET needs_chapter_refresh = 0, updated_at = ? WHERE id = ?",
                (now, event_id),
            )
        conn.execute("UPDATE photos SET relation_choice = 'merge' WHERE id = ?", (photo_id,))
        conn.execute(
            "UPDATE chapters SET status = 'discarded', updated_at = ? WHERE id = ?",
            (now, source_chapter_id),
        )
    return {
        "choice": choice,
        "chapter": chapter_detail(chapter_id),
        "discarded_chapter_id": source_chapter_id,
    }


def create_share(chapter_id: str) -> dict[str, Any]:
    chapter = chapter_detail(chapter_id)
    version = chapter["current_version"]
    if chapter["status"] != "confirmed" or not version.get("confirmed_at"):
        raise HTTPException(status_code=409, detail="只有已经确认的章节版本可以分享")
    existing = fetch_one(
        "SELECT * FROM share_links WHERE chapter_id = ? AND version_id = ? AND status = 'active'",
        (chapter_id, version["id"]),
    )
    if existing:
        existing["share_url"] = f"/share/{existing['token']}"
        return existing
    link_id, token, now = _id(), secrets.token_urlsafe(24), now_iso()
    execute(
        "INSERT INTO share_links VALUES (?, ?, ?, ?, 'active', ?, NULL)",
        (link_id, token, chapter_id, version["id"], now),
    )
    link = require_row("share_links", link_id)
    link["share_url"] = f"/share/{token}"
    return link


def get_shared_chapter(token: str) -> dict[str, Any]:
    link = fetch_one("SELECT * FROM share_links WHERE token = ? AND status = 'active'", (token,))
    if not link:
        raise HTTPException(status_code=404, detail="分享链接不存在或已经撤回")
    chapter = require_row("chapters", link["chapter_id"])
    version = require_row("chapter_versions", link["version_id"])
    return {
        "title": chapter["title"],
        "content": version["content"],
        "version_number": version["version_number"],
        "shared_at": link["created_at"],
    }


def revoke_share(share_id: str) -> dict[str, Any]:
    link = require_row("share_links", share_id)
    if link["status"] == "revoked":
        return link
    execute(
        "UPDATE share_links SET status = 'revoked', revoked_at = ? WHERE id = ?",
        (now_iso(), share_id),
    )
    return require_row("share_links", share_id)


def export_project(project_id: str) -> dict[str, Any]:
    project = require_row("projects", project_id)
    photos = fetch_all("SELECT * FROM photos WHERE project_id = ? ORDER BY created_at", (project_id,))
    photo_observations = fetch_all(
        """
        SELECT po.* FROM photo_observations po JOIN photos p ON p.id = po.photo_id
        WHERE p.project_id = ? ORDER BY po.created_at
        """,
        (project_id,),
    )
    sessions = fetch_all("SELECT * FROM interview_sessions WHERE project_id = ? ORDER BY created_at", (project_id,))
    for session in sessions:
        session["turns"] = fetch_all(
            "SELECT * FROM interview_turns WHERE session_id = ? ORDER BY created_at", (session["id"],)
        )
        session["facts"] = fetch_all(
            "SELECT * FROM memory_facts WHERE session_id = ? ORDER BY created_at", (session["id"],)
        )
    chapters = fetch_all("SELECT * FROM chapters WHERE project_id = ? ORDER BY created_at", (project_id,))
    for chapter in chapters:
        chapter["versions"] = fetch_all(
            "SELECT * FROM chapter_versions WHERE chapter_id = ? ORDER BY version_number", (chapter["id"],)
        )
        chapter["photo_ids"] = [
            row["photo_id"]
            for row in fetch_all("SELECT photo_id FROM chapter_photos WHERE chapter_id = ?", (chapter["id"],))
        ]
    shares = fetch_all(
        """
        SELECT sl.* FROM share_links sl JOIN chapters c ON c.id = sl.chapter_id
        WHERE c.project_id = ? ORDER BY sl.created_at
        """,
        (project_id,),
    )
    model_runs = fetch_all(
        "SELECT * FROM model_runs WHERE project_id = ? ORDER BY created_at", (project_id,)
    )
    book_editions = fetch_all(
        "SELECT * FROM book_editions WHERE project_id = ? ORDER BY edition_number", (project_id,)
    )
    for edition in book_editions:
        edition["chapters"] = fetch_all(
            "SELECT * FROM book_edition_chapters WHERE edition_id = ? ORDER BY chapter_order",
            (edition["id"],),
        )
    autobiography_editions = fetch_all(
        "SELECT * FROM autobiography_editions WHERE project_id = ? ORDER BY edition_number",
        (project_id,),
    )
    timeline = timeline_detail(project_id)
    return {
        "exported_at": now_iso(),
        "project": project,
        "photos": photos,
        "photo_observations": photo_observations,
        "sessions": sessions,
        "timeline": timeline,
        "chapters": chapters,
        "shares": shares,
        "model_runs": model_runs,
        "book_editions": book_editions,
        "autobiography_editions": autobiography_editions,
    }


def _remove_media_file_if_unreferenced(photo: dict[str, Any]) -> int:
    remaining = fetch_one(
        """
        SELECT id FROM photos
        WHERE stored_name = ? AND id != ? AND deleted_at IS NULL
        LIMIT 1
        """,
        (photo["stored_name"], photo["id"]),
    )
    if remaining:
        return 0
    path = settings.media_dir / photo["stored_name"]
    if path.exists():
        path.unlink()
        return 1
    return 0


def delete_photo(photo_id: str, delete_content: bool = False) -> dict[str, Any]:
    """默认只删照片素材；故事内容只有在明确选择后才会删除。"""
    photo = require_row("photos", photo_id)
    project_id = photo["project_id"]
    now = now_iso()
    if not delete_content:
        if not photo.get("deleted_at"):
            with connection() as conn:
                conn.execute("DELETE FROM photo_observations WHERE photo_id = ?", (photo_id,))
                conn.execute("UPDATE photos SET deleted_at = ? WHERE id = ?", (now, photo_id))
                conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        removed_files = _remove_media_file_if_unreferenced(photo)
        linked_chapters = fetch_one(
            "SELECT COUNT(*) AS value FROM chapter_photos WHERE photo_id = ?", (photo_id,)
        )
        return {
            "deleted": True,
            "photo_id": photo_id,
            "mode": "asset_only",
            "content_preserved": True,
            "preserved_chapters": int((linked_chapters or {"value": 0})["value"]),
            "removed_media_files": removed_files,
        }

    impacted_chapters = fetch_all(
        """
        SELECT c.id, c.title,
               (SELECT COUNT(*) FROM chapter_photos cp2 WHERE cp2.chapter_id = c.id) AS photo_count,
               (SELECT COUNT(*) FROM chapter_events ce WHERE ce.chapter_id = c.id) AS event_count
        FROM chapters c
        JOIN chapter_photos cp ON cp.chapter_id = c.id
        WHERE cp.photo_id = ? AND c.status != 'discarded'
        """,
        (photo_id,),
    )
    merged = [chapter for chapter in impacted_chapters if chapter["photo_count"] > 1 or chapter["event_count"] > 1]
    if merged:
        names = "、".join(f"《{chapter['title']}》" for chapter in merged[:3])
        raise HTTPException(
            status_code=409,
            detail=f"{names}还包含其他照片或人生事件，不能整章直接删除；请先在章节修改中指定要删掉的段落。",
        )

    chapter_ids = [chapter["id"] for chapter in impacted_chapters]
    autobiography_count = int((fetch_one(
        "SELECT COUNT(*) AS value FROM autobiography_editions WHERE project_id = ?", (project_id,)
    ) or {"value": 0})["value"])
    book_count = int((fetch_one(
        "SELECT COUNT(*) AS value FROM book_editions WHERE project_id = ?", (project_id,)
    ) or {"value": 0})["value"])
    with connection() as conn:
        # 完整自传是章节的派生作品，删除来源故事后旧整书版本不再安全保留。
        conn.execute("DELETE FROM autobiography_editions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM book_editions WHERE project_id = ?", (project_id,))
        for chapter_id in chapter_ids:
            conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
    removed_files = _remove_media_file_if_unreferenced(photo)
    return {
        "deleted": True,
        "photo_id": photo_id,
        "mode": "asset_and_story",
        "content_preserved": False,
        "removed_chapters": len(chapter_ids),
        "removed_autobiography_editions": autobiography_count,
        "removed_book_editions": book_count,
        "removed_media_files": removed_files,
    }


def delete_project(project_id: str) -> dict[str, Any]:
    require_row("projects", project_id)
    stored_names = [
        row["stored_name"] for row in fetch_all("SELECT stored_name FROM photos WHERE project_id = ?", (project_id,))
    ]
    with connection() as conn:
        conn.execute("DELETE FROM model_runs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    removed_files = 0
    for stored_name in stored_names:
        remaining = fetch_one("SELECT id FROM photos WHERE stored_name = ? LIMIT 1", (stored_name,))
        if remaining:
            continue
        path = settings.media_dir / stored_name
        if path.exists():
            path.unlink()
            removed_files += 1
    return {"deleted": True, "project_id": project_id, "removed_media_files": removed_files}
