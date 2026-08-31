from __future__ import annotations

import re
from typing import Any


_YEAR_RANGE_PATTERN = re.compile(r"((?:18|19|20|21)\d{2})\s*年?\s*(?:到|至|—|-|~)\s*((?:18|19|20|21)\d{2})")
_DECADE_PATTERN = re.compile(r"((?:18|19|20|21)\d{2})\s*年代")
_CENTURY_PATTERN = re.compile(r"(1[89]|2[01])\s*世纪\s*(初|前期|前半|中前期|中期|中后期|后半|末期|末|上半叶|下半叶)?")
_EXPLICIT_COUNT_PATTERN = re.compile(
    r"(?:我们|我俩|我们俩|我们仨|总共|一共|只有|就)\s*"
    r"(两|三|四|五|六|七|八|九|十|\d{1,2})\s*(?:个|位|口|人)"
)
_COUNT_WORDS = {"两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def confirmed_place_texts(
    facts: list[dict[str, Any]],
    event_location: str | None = None,
) -> list[str]:
    """收集用户已确认的地点文本：place 类事实与事件地点。"""
    places: list[str] = []
    for fact in facts or []:
        if str(fact.get("fact_type", "")) == "place":
            value = str(fact.get("value") or "").strip()
            if value:
                places.append(value)
    location = str(event_location or "").strip()
    if location:
        places.append(location)
    return places


def _overlaps(text: str, corpus: str) -> bool:
    return any(ch in corpus for ch in text if not ch.isspace())


def _explicit_user_count(user_texts: list[str]) -> int | None:
    for text in user_texts:
        compact = re.sub(r"\s+", "", str(text or ""))
        match = _EXPLICIT_COUNT_PATTERN.search(compact)
        if match:
            word = match.group(1)
            if word.isdigit():
                return int(word)
            if word in _COUNT_WORDS:
                return _COUNT_WORDS[word]
    return None


def _century_range(century: str, qualifier: str | None) -> tuple[int, int] | None:
    start = int(century) * 100 - 100
    if qualifier in {"初", "前期", "前半", "上半叶"}:
        return start + 0, start + 40
    if qualifier in {"中前期"}:
        return start + 20, start + 60
    if qualifier in {"中期", "中后期"}:
        return start + 40, start + 80
    if qualifier in {"末期", "末", "后半", "下半叶"}:
        return start + 60, start + 99
    return start + 0, start + 99


def _clue_years(value: str) -> tuple[int, int] | None:
    compact = re.sub(r"\s+", "", value or "")
    range_match = _YEAR_RANGE_PATTERN.search(compact)
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))
    decade_match = _DECADE_PATTERN.search(compact)
    if decade_match:
        start = int(decade_match.group(1))
        return start, start + 9
    century_match = _CENTURY_PATTERN.search(compact)
    if century_match:
        return _century_range(century_match.group(1), century_match.group(2))
    years = re.findall(r"(?<!\d)((?:18|19|20|21)\d{2})", compact)
    if years:
        first = int(years[0])
        return first, first
    return None


def confirmed_years(facts: list[dict[str, Any]]) -> list[int]:
    years: list[int] = []
    for fact in facts or []:
        if str(fact.get("fact_type", "")) != "time":
            continue
        for match in re.findall(r"(?<!\d)((?:18|19|20|21)\d{2})", str(fact.get("value") or "")):
            years.append(int(match))
    return years


def arbitrate_observation(
    data: dict[str, Any],
    confirmed_places: list[str],
    confirmed_years: list[int] | None = None,
    user_texts: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """证据仲裁：视觉候选与用户已确认事实矛盾时，候选被否决。

    地点互斥：确认了地点之后，与确认地点无字符重叠的视觉地点候选按误识别处理；
    该候选依据（basis）中出现的具体物件（如“埃菲尔铁塔”）一并否决。
    时间互斥：确认了明确年份后，区间不包含任何确认年份的视觉时间候选被否决。
    人物数量：用户明确说了人数且与视觉识别不一致时，只保留提示、不删人物描述。
    返回（仲裁后的 observations 副本, 被否决的术语列表）。
    """
    data = data or {}
    forbidden: list[str] = []
    notes: list[str] = []
    if confirmed_places:
        corpus = "".join(confirmed_places)
        for clue in data.get("place_clues") or []:
            if not isinstance(clue, dict):
                continue
            value = str(clue.get("value") or "").strip()
            basis = str(clue.get("basis") or "").strip()
            if not value or _overlaps(value, corpus):
                continue
            forbidden.append(value)
            for obj in data.get("objects") or []:
                text = str(obj or "").strip()
                if text and text in basis and text not in forbidden:
                    forbidden.append(text)
        if forbidden:
            notes.append(
                "视觉模型识别的地点与用户确认的地点（"
                + "、".join(confirmed_places)
                + "）不一致，已按视觉误识别处理；写作时背景描写只用已确认地点，"
                "不得出现被否决的具体地标名称。"
            )
    years = confirmed_years or []
    if years:
        kept_clues = []
        for clue in data.get("time_clues") or []:
            if not isinstance(clue, dict):
                kept_clues.append(clue)
                continue
            value = str(clue.get("value") or "").strip()
            span = _clue_years(value) if value else None
            if span and not any(span[0] <= year <= span[1] for year in years):
                if value not in forbidden:
                    forbidden.append(value)
                notes.append("视觉时间线索与用户确认的年份不一致，已按误识别处理。")
                continue
            kept_clues.append(clue)
        if len(kept_clues) != len(data.get("time_clues") or []):
            data = {**data, "time_clues": kept_clues}
    people_count = data.get("people_count")
    user_count = _explicit_user_count(user_texts or []) if isinstance(people_count, int) else None
    if user_count is not None and people_count is not None and int(people_count) != user_count:
        notes.append(
            f"视觉识别画面中约{people_count}人，用户讲述为{user_count}人；"
            "人物数量以用户讲述为准，画面人物描述不得用来认定在场人数。"
        )
    if not forbidden and not notes:
        return data, []
    filtered = {**data}
    if forbidden:
        filtered["place_clues"] = [
            clue for clue in (data.get("place_clues") or [])
            if not (isinstance(clue, dict) and str(clue.get("value") or "").strip() in forbidden)
        ]
        filtered["objects"] = [
            obj for obj in (data.get("objects") or [])
            if str(obj or "").strip() not in forbidden
        ]
    if notes:
        filtered["arbitration_notes"] = notes
    return filtered, forbidden


def entity_place_conflicts(
    entities: list[dict[str, Any]] | None,
    confirmed_places: list[str],
) -> list[str]:
    """声明式实体检查：地点/地标类实体与确认地点无重叠时视为冲突。"""
    if not entities or not confirmed_places:
        return []
    corpus = "".join(confirmed_places)
    conflicts: list[str] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if str(entity.get("type", "")) not in {"place", "landmark"}:
            continue
        name = str(entity.get("name") or "").strip()
        if not name or _overlaps(name, corpus):
            continue
        if name not in conflicts:
            conflicts.append(name)
    return conflicts
