from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Any

from .config import settings
from .db import connection, fetch_all, fetch_one, now_iso


_CJK_PATTERN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SNAPSHOT_SCHEMA_VERSION = 4
_RELATION_WORDS = (
    "父亲", "母亲", "爸爸", "妈妈", "丈夫", "妻子", "爱人", "老伴",
    "女儿", "儿子", "孩子", "外孙女", "孙女", "老师", "师傅", "组长",
    "同学", "朋友", "同事", "工友", "姐姐", "妹妹", "哥哥", "弟弟",
)


def estimate_tokens(value: Any) -> int:
    """不依赖特定供应商 tokenizer 的保守估算；中文按一字约一 token 计算。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    other = len(re.sub(r"[\s\u3400-\u9fff\uf900-\ufaff]", "", text))
    return max(1, cjk + math.ceil(other / 3))


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _excerpt(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", "", text or "").strip()
    return compact if len(compact) <= limit else compact[:limit].rstrip("，、；：") + "……"


def _snapshot_sources(project_id: str) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]],
]:
    project = fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return {}, [], [], [], None, None, []
    facts = fetch_all(
        """
        SELECT id, fact_type, value, status, sensitivity, include_in_book, event_id, created_at
        FROM memory_facts WHERE project_id = ? AND status != 'retracted' AND include_in_book = 1
        ORDER BY created_at, rowid
        """,
        (project_id,),
    )
    events = fetch_all(
        """
        SELECT id, title, time_text, start_year, end_year, time_precision, location, summary, status, updated_at
        FROM timeline_events WHERE project_id = ?
        ORDER BY CASE WHEN start_year IS NULL THEN 1 ELSE 0 END, start_year, created_at, rowid
        """,
        (project_id,),
    )
    chapters = fetch_all(
        """
        SELECT c.id, c.title, c.status, c.current_version_id, cv.content
        FROM chapters c LEFT JOIN chapter_versions cv ON cv.id = c.current_version_id
        WHERE c.project_id = ? AND c.status != 'discarded' ORDER BY c.created_at, c.rowid
        """,
        (project_id,),
    )
    autobiography = fetch_one(
        """
        SELECT id, title, core_theme, character_portrait, manuscript_json, source_snapshot_json
        FROM autobiography_editions WHERE project_id = ? ORDER BY edition_number DESC LIMIT 1
        """,
        (project_id,),
    )
    people_catalog = fetch_one("SELECT * FROM people_catalogs WHERE project_id = ?", (project_id,))
    event_mentions = fetch_all(
        """
        SELECT id, linked_event_id, temporal_role, time_text, start_year, end_year,
               time_precision, life_stage, source_type, source_id, raw_text, link_status, created_at
        FROM event_mentions WHERE project_id = ? ORDER BY created_at, rowid
        """,
        (project_id,),
    )
    return project, facts, events, chapters, autobiography, people_catalog, event_mentions


def _source_fingerprint(
    project: dict[str, Any],
    facts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    autobiography: dict[str, Any] | None,
    people_catalog: dict[str, Any] | None,
    event_mentions: list[dict[str, Any]],
) -> str:
    source = {
        "snapshot_schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "project": [project.get("title"), project.get("narrative_person"), project.get("updated_at")],
        "facts": [[row.get("id"), row.get("status"), row.get("value"), row.get("event_id")] for row in facts],
        "events": [[row.get("id"), row.get("title"), row.get("time_text"), row.get("summary"), row.get("updated_at")] for row in events],
        "event_mentions": [[row.get("id"), row.get("linked_event_id"), row.get("temporal_role"), row.get("time_text"), row.get("link_status")] for row in event_mentions],
        "chapters": [[row.get("id"), row.get("current_version_id"), row.get("status")] for row in chapters],
        "autobiography": (autobiography or {}).get("id"),
        "people_catalog": (people_catalog or {}).get("source_fingerprint"),
    }
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def get_life_context_snapshot(project_id: str) -> dict[str, Any]:
    """生成所有 Agent 共用、可版本追踪的人生骨架，不把生成正文升级为事实。"""
    project, facts, events, chapters, autobiography, people_catalog, event_mentions = _snapshot_sources(project_id)
    if not project:
        return {}
    fingerprint = _source_fingerprint(
        project, facts, events, chapters, autobiography, people_catalog, event_mentions
    )
    cached = fetch_one(
        """
        SELECT * FROM life_context_snapshots
        WHERE project_id = ? AND source_fingerprint = ? ORDER BY version_number DESC LIMIT 1
        """,
        (project_id, fingerprint),
    )
    if cached:
        return _json_object(cached.get("snapshot_json"))

    name_candidates: list[str] = []
    title_match = re.match(r"^([\u4e00-\u9fff]{2,4})(?=[:：·])", str(project.get("title") or ""))
    if title_match:
        name_candidates.append(title_match.group(1))
    for event in events:
        name_candidates.extend(re.findall(
            r"([\u4e00-\u9fff]{2,4})(?=\d{1,3}岁)",
            f"{event.get('time_text', '')} {event.get('title', '')}",
        ))
    protagonist_name = max(set(name_candidates), key=name_candidates.count) if name_candidates else "主人公"

    catalog = _json_object((people_catalog or {}).get("catalog_json"))
    important_people = []
    for person in catalog.get("people", []):
        if person.get("kind") == "visual_unknown":
            continue
        important_people.append({
            "person_id": person.get("id"),
            "name": person.get("display_name"),
            "aliases": person.get("aliases", []),
            "relationship": person.get("relationship"),
            "key_attributes": person.get("key_attributes", []),
            "shared_experience": _excerpt(str(person.get("summary") or ""), 180),
            "event_ids": person.get("event_ids", []),
        })
        if len(important_people) >= settings.life_snapshot_max_people:
            break

    relationship_evidence = [
        {
            "fact_id": fact["id"],
            "event_id": fact.get("event_id"),
            "text": _excerpt(str(fact.get("value") or ""), 180),
            "status": fact.get("status"),
        }
        for fact in facts
        if fact.get("fact_type") == "person" or any(word in str(fact.get("value") or "") for word in _RELATION_WORDS)
    ][:40]
    known_people = {
        (str(person.get("name") or ""), str(person.get("relationship") or ""))
        for person in important_people
    }
    known_relations = {
        str(person.get("relationship") or "") for person in important_people
    }
    family_relations = {
        "父亲", "母亲", "爸爸", "妈妈", "丈夫", "妻子", "爱人", "老伴",
        "女儿", "儿子", "外孙女", "孙女",
    }
    role_relations = {"老师", "师傅", "组长"}
    for fact in facts:
        value = str(fact.get("value") or "")
        relation = next((word for word in _RELATION_WORDS if word in value), None)
        if not relation:
            continue
        canonical_relation = {
            "爸爸": "父亲", "妈妈": "母亲", "丈夫": "爱人", "妻子": "爱人", "老伴": "爱人",
            "孩子": "子女", "孙女": "外孙女", "工友": "同事",
        }.get(relation, relation)
        # 已由人物档案覆盖的关系不再靠正则补人名；正则只承担新项目的冷启动兜底。
        if canonical_relation in known_relations:
            continue
        display_name = ""
        if relation in family_relations:
            # 仅接受有明确语法边界的亲属姓名，避免把“师傅腰伤住院”中的动作误识别人名。
            name_match = re.search(
                rf"(?:我的|我)?{re.escape(relation)}(?:叫|名叫)?([\u4e00-\u9fff]{{2,4}}?)(?=在|是|于|生|坐|站|会|和|与|，|。|；|\d)",
                value,
            )
            if name_match:
                display_name = name_match.group(1)
            else:
                reverse_match = re.search(
                    rf"(?:^|[，。；、\s])([\u4e00-\u9fff]{{2,4}}?)(?:是)?我(?:的)?{re.escape(relation)}(?=，|。|；|\s|$)",
                    value,
                )
                if reverse_match:
                    display_name = reverse_match.group(1)
        elif relation in role_relations:
            honorific_match = re.search(rf"(?:^|[，。；、\s])([\u4e00-\u9fff]{re.escape(relation)})", value)
            if honorific_match:
                display_name = honorific_match.group(1)

        # 未抽出明确姓名时，只在该关系尚不存在的情况下保留一个关系词条。
        if not display_name:
            if canonical_relation in known_relations:
                continue
            display_name = canonical_relation
        key = (display_name, canonical_relation)
        if key in known_people:
            continue
        important_people.append({
            "person_id": None,
            "name": display_name,
            "aliases": [],
            "relationship": canonical_relation,
            "key_attributes": [canonical_relation],
            "shared_experience": _excerpt(value, 180),
            "event_ids": [fact.get("event_id")] if fact.get("event_id") else [],
        })
        known_people.add(key)
        known_relations.add(canonical_relation)
        if len(important_people) >= settings.life_snapshot_max_people:
            break

    mentions_by_event: dict[str, list[dict[str, Any]]] = {}
    for mention in event_mentions:
        mentions_by_event.setdefault(str(mention.get("linked_event_id") or ""), []).append(mention)
    important_events = []
    for event in events[: settings.life_snapshot_max_events]:
        important_events.append({
            "event_id": event["id"],
            "time": event.get("time_text"),
            "start_year": event.get("start_year"),
            "title": event.get("title"),
            "location": event.get("location"),
            "summary": _excerpt(str(event.get("summary") or ""), 240),
            "status": event.get("status"),
            "related_event_mentions": [
                {
                    "mention_id": mention.get("id"),
                    "role": mention.get("temporal_role"),
                    "time": mention.get("time_text"),
                    "start_year": mention.get("start_year"),
                    "description": _excerpt(str(mention.get("raw_text") or ""), 160),
                    "source_type": mention.get("source_type"),
                }
                for mention in mentions_by_event.get(event["id"], [])
                if mention.get("link_status") == "related"
            ][:8],
        })

    fact_priority = {"person": 0, "time": 1, "reflection": 2, "event": 3, "feeling": 4, "place": 5, "quote": 6, "other": 7}
    key_facts = sorted(
        facts,
        key=lambda fact: (
            0 if fact.get("status") == "confirmed_by_user" else 1,
            fact_priority.get(str(fact.get("fact_type")), 9),
            str(fact.get("created_at") or ""),
        ),
    )[:50]

    narrative_memory: dict[str, Any] = {
        "authority": "narrative_derivative_not_fact",
        "chapters": [
            {
                "chapter_id": chapter["id"],
                "title": chapter.get("title"),
                "status": chapter.get("status"),
                "excerpt": _excerpt(str(chapter.get("content") or ""), 180),
            }
            for chapter in chapters[:30]
        ],
    }
    if autobiography:
        manuscript = _json_object(autobiography.get("manuscript_json"))
        narrative_memory.update({
            "autobiography_id": autobiography.get("id"),
            "title": autobiography.get("title"),
            "core_theme": autobiography.get("core_theme"),
            "character_portrait": _excerpt(str(autobiography.get("character_portrait") or ""), 360),
            "sections": [
                {
                    "title": section.get("title"),
                    "source_chapter_ids": section.get("source_chapter_ids", []),
                    "character_revelation": section.get("character_revelation", ""),
                    "narrative_function": section.get("narrative_function", ""),
                }
                for section in manuscript.get("sections", [])[:20]
            ],
        })

    previous = fetch_one(
        "SELECT COALESCE(MAX(version_number), 0) AS value FROM life_context_snapshots WHERE project_id = ?",
        (project_id,),
    ) or {"value": 0}
    version_number = int(previous["value"]) + 1
    snapshot_id = str(uuid.uuid4())
    snapshot = {
        "snapshot_id": snapshot_id,
        "version": version_number,
        "project_id": project_id,
        "protagonist": {
            "name": protagonist_name,
            "narrative_person": project.get("narrative_person"),
        },
        "important_people": important_people,
        "relationship_evidence": relationship_evidence,
        "important_events": important_events,
        "key_facts": [
            {
                "fact_id": fact["id"],
                "type": fact.get("fact_type"),
                "value": fact.get("value"),
                "event_id": fact.get("event_id"),
                "status": fact.get("status"),
            }
            for fact in key_facts
        ],
        "conflicts": [
            {"fact_id": fact["id"], "value": fact.get("value"), "event_id": fact.get("event_id")}
            for fact in facts if fact.get("status") == "disputed"
        ],
        "narrative_memory": narrative_memory,
        "memory_rules": {
            "fact_authority": "人物、时间、地点、关系和事件结果只以 key_facts 与原始证据为准",
            "narrative_boundary": "narrative_memory 只用于主题、人物发展和前后呼应，不能建立新事实",
        },
        "source_fingerprint": fingerprint,
        "generated_at": now_iso(),
    }
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO life_context_snapshots
            (id, project_id, version_number, source_fingerprint, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (snapshot_id, project_id, version_number, fingerprint, json.dumps(snapshot, ensure_ascii=False), now_iso()),
        )
    return snapshot


def latest_compaction(session_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT * FROM conversation_compactions WHERE session_id = ? ORDER BY version_number DESC LIMIT 1",
        (session_id,),
    )
    if row:
        row["summary"] = _json_object(row.get("summary_json"))
    return row


def save_compaction(
    session_id: str,
    source_turn_count: int,
    source_through_turn_id: str | None,
    source_token_count: int,
    summary: dict[str, Any],
) -> dict[str, Any]:
    previous = latest_compaction(session_id)
    version = int(previous.get("version_number") or 0) + 1 if previous else 1
    summary_json = json.dumps(summary, ensure_ascii=False)
    row_id = str(uuid.uuid4())
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO conversation_compactions
            (id, session_id, version_number, source_turn_count, source_through_turn_id,
             source_token_count, compressed_token_count, summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id, session_id, version, source_turn_count, source_through_turn_id,
                source_token_count, estimate_tokens(summary_json), summary_json, now_iso(),
            ),
        )
    return latest_compaction(session_id) or {}


def select_recent_turns(turns: list[dict[str, Any]], token_budget: int, max_turns: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for turn in reversed(turns):
        cost = estimate_tokens({"role": turn.get("role"), "content": turn.get("content")})
        if selected and (len(selected) >= max(1, max_turns) or used + cost > max(200, token_budget)):
            break
        selected.append(turn)
        used += cost
    return list(reversed(selected))


def context_status(session_id: str) -> dict[str, Any]:
    latest = latest_compaction(session_id)
    return {
        "trigger_tokens": settings.context_compression_trigger_tokens,
        "target_tokens": settings.context_compression_target_tokens,
        "compressed": bool(latest),
        "compaction_version": latest.get("version_number") if latest else None,
        "source_turn_count": latest.get("source_turn_count", 0) if latest else 0,
        "source_token_count": latest.get("source_token_count", 0) if latest else 0,
        "compressed_token_count": latest.get("compressed_token_count", 0) if latest else 0,
    }
