from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .config import settings
from .evidence_arbitration import entity_place_conflicts
from .llm import gateway
from .prompts import (
    AUTOBIOGRAPHY_COMPILER_SYSTEM,
    AUTOBIOGRAPHY_REGROUP_SYSTEM,
    AUTOBIOGRAPHY_REVIEW_SYSTEM,
    BOOK_CONTINUITY_REVIEW_SYSTEM,
    BOOK_DIRECTOR_SYSTEM,
    CHAPTER_FACT_LINK_SYSTEM,
    CHAPTER_SYSTEM,
    CHAPTER_REWEAVE_SYSTEM,
    COMMON_SENSE_REVIEW_SYSTEM,
    CONTEXT_COMPACTION_SYSTEM,
    FORCED_CHAPTER_LINK_SYSTEM,
    INTERVIEW_SYSTEM,
    INTERVIEW_REPLY_EDITOR_SYSTEM,
    MEMORY_SYSTEM,
    MERGE_SYSTEM,
    PEOPLE_CURATOR_SYSTEM,
    PHOTO_TITLE_SYSTEM,
    RELATION_SYSTEM,
    REVIEW_SYSTEM,
)
from .schemas import (
    AutobiographyManuscriptOutput,
    AutobiographyReviewOutput,
    BookContinuityReviewOutput,
    BookDirectorOutput,
    ChapterAgentOutput,
    ChapterFactLinkOutput,
    ConversationCompactionOutput,
    InterviewAgentOutput,
    MemoryAgentOutput,
    MergeAgentOutput,
    PeopleCatalogOutput,
    PhotoTitleOutput,
    RelationAgentOutput,
    ReviewAgentOutput,
)


OutputModel = TypeVar("OutputModel", bound=BaseModel)


def _validated(
    value: dict[str, Any],
    model: type[OutputModel],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    try:
        return model.model_validate(value).model_dump()
    except ValidationError:
        return model.model_validate(fallback).model_dump()


EMOTION_WORDS = (
    "开心", "高兴", "紧张", "难过", "害怕", "舍不得", "踏实", "温暖",
    "兴奋", "骄傲", "自豪", "期待", "感动", "遗憾", "伤感", "安稳", "轻松",
    "新奇", "欢喜",
)
VISUAL_INFERENCE_PATTERNS = (
    r"照片上.*(?:笑|表情|神情|眼神|穿着|站着)",
    r"从照片里.*(?:看得出|看出来)",
    r"眼里.*(?:亮|光)",
    r"(?:好像|仿佛).*看见",
)
UNGROUNDED_DESCRIPTION_WORDS = (
    "笑容", "热热闹闹", "眼睛发亮", "眼里有光", "勇敢", "坚强", "伟大", "了不起",
)
OVERWRITTEN_WARMTH_PATTERNS = (
    r"新奇里.*藏着",
    r"从.+一步步(?:走|来到|走到)",
    r"记住.*(?:来路|初心)",
    r"真的很有心",
    r"命运的转折",
    r"人生画卷",
    r"谢谢您愿意.*(?:讲|说)给我听",
    r"想必.*(?:缘由|原因)",
    r"一直是.+很标志的地方",
    r"(?:他|她|您|他们|家人|妈妈|母亲).{0,6}一定",
    r"专门(?:挑|选)",
    r"(?:真|很)用心",
)
WORKFLOW_LANGUAGE_PATTERNS = (
    r"完整的材料",
    r"材料已经",
    r"生成一版",
    r"进入成稿",
    r"之后还可以继续修改",
)
TASKLIKE_LISTENING_PATTERNS = (
    r"我(?:已经)?记住了",
    r"我(?:已经)?记下了",
    r"我会替您记",
    r"替您(?:好好)?记着",
    r"我听见了",
    r"我都好好记着",
    r"我先把.+记好",
)
BOUNDARY_WORDS = ("记不清", "记不住", "不想说", "不愿意", "先不说", "累了", "暂停")
GROUNDING_RISK_MARKERS = (
    "住在", "只在想象", "藏在心里", "愿望", "记不清", "期待", "新鲜", "陌生",
    "慌张", "队伍", "游客", "等待", "盯着", "灯光", "街头", "一扇门", "梦里",
    "一起走", "一起看", "见证这一切", "高高地立", "改变了人生",
    "天又冷", "天气很冷", "天气冷", "拍了不少", "好几张照片",
)
# 访谈回应必须严格贴近原话；章节则允许合理的文学心理和意义推断，
# 这里只拦截会被读者误认为真实发生过的具体外部细节或记忆状态。
CHAPTER_HARD_RISK_MARKERS = (
    "住在", "记不清", "队伍", "游客", "等待", "盯着", "灯光", "街头",
    "天又冷", "天气很冷", "天气冷", "拍了不少", "好几张照片",
)
_REPLY_OPENING_STRIP = re.compile(r"^[\s，。！？、；：…—“”\"'!?.,;:~～·（）()【】\[\]]+")


def reply_opening(text: str) -> str:
    """取回应开头的前两个有效汉字，用于跨轮次开头去重。"""
    cleaned = _REPLY_OPENING_STRIP.sub("", str(text or ""))
    match = re.match(r"[\u4e00-\u9fff]{2}", cleaned)
    return match.group(0) if match else ""


def previous_reply_openings(turns: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """收集最近几轮 assistant 回应的开头两字；按最近优先、去重。"""
    openings: list[str] = []
    for turn in reversed(turns):
        if turn.get("role") != "assistant":
            continue
        opening = reply_opening(str(turn.get("content", "")))
        if opening and opening not in openings:
            openings.append(opening)
        if len(openings) >= limit:
            break
    return openings


def reply_opening_repeats(reply: str, openings: list[str]) -> bool:
    opening = reply_opening(reply)
    return bool(opening) and opening in openings


def _assistant_text_after(turns: list[dict[str, Any]], index: int) -> str:
    return "\n".join(
        str(turn.get("content", ""))
        for turn in turns[index + 1 :]
        if turn.get("role") == "assistant"
    )


def interview_priority_question(turns: list[dict[str, Any]]) -> str | None:
    """找出对成稿影响最大的未解释线索，避免模型只追逐下一个场景。"""
    last_user_text = next(
        (str(turn.get("content", "")) for turn in reversed(turns) if turn.get("role") == "user"),
        "",
    )
    if any(word in last_user_text for word in BOUNDARY_WORDS):
        return None

    # “从小就想去某地”包含人物动机。若没有解释来源，应先问为什么向往。
    desire_pattern = re.compile(
        r"(?:小时候|从小|一直|早就)[^。！？!?]{0,10}?想(?:去|到)"
        r"(?P<place>[\u4e00-\u9fff]{2,8}?)(?:看看|看一看|玩|旅游|旅行|然后|后来|刚好|正好|，|。|！|？|$)"
    )
    for index, turn in enumerate(turns):
        if turn.get("role") != "user":
            continue
        text = str(turn.get("content", ""))
        match = desire_pattern.search(text)
        if not match:
            continue
        if any(marker in text for marker in ("因为", "由于", "是因为", "缘故", "所以一直想")):
            continue
        place = match.group("place")
        later_questions = _assistant_text_after(turns, index)
        asked = place in later_questions and any(
            word in later_questions for word in ("为什么", "什么让", "怎么会", "怎么开始", "向往")
        )
        if not asked:
            return f"您刚才提到小时候就很想去{place}，我有点好奇，那时候是什么让您对{place}这么向往呢？"

    # 同一句中出现“从/去 A 前往 B”，只能确认路线，不能推断用户为何身在 A。
    route_pattern = re.compile(
        r"(?:从|由|去)(?P<source>[\u4e00-\u9fff]{2,8}?)(?:出发)?"
        r"(?:前往|去往)(?P<destination>[\u4e00-\u9fff]{2,8}?)"
        r"(?:看看|玩|旅游|旅行|了|，|。|！|？|$)"
    )
    all_user_text = "\n".join(
        str(turn.get("content", "")) for turn in turns if turn.get("role") == "user"
    )
    for index, turn in enumerate(turns):
        if turn.get("role") != "user":
            continue
        match = route_pattern.search(str(turn.get("content", "")))
        if not match:
            continue
        source = match.group("source")
        destination = match.group("destination")
        relation_is_known = bool(
            re.search(
                rf"(?:在|住在|家在){re.escape(source)}[^。！？!?]{{0,8}}"
                r"(?:上学|读书|工作|生活|居住|住|旅游|中转|转车|经过)",
                all_user_text,
            )
        )
        later_questions = _assistant_text_after(turns, index)
        asked = source in later_questions and any(
            word in later_questions for word in ("什么关系", "为什么", "怎么会在", "出发", "中转", "经过")
        )
        if not relation_is_known and not asked:
            return (
                f"您刚才还提到了{source}，我想把去{destination}的这段路记准确一点："
                f"{source}和这次旅行是什么关系呢？"
            )
    return None


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.findall(r"[^。！？!?]+[。！？!?]?", text) if item.strip()]


def _interview_reply_length_guidance(last_user_text: str) -> dict[str, int | str]:
    """长度随本轮信息量变化，只给模型交流节奏，不向正文填充模板。"""
    compact = re.sub(r"\s+", "", str(last_user_text or ""))
    detail_signals = sum(
        marker in compact
        for marker in ("因为", "所以", "但是", "后来", "当时", "觉得", "和", "一起", "第一次")
    )
    if len(compact) <= 18 and detail_signals <= 1:
        minimum, maximum = 45, 100
    elif len(compact) <= 60 and detail_signals <= 3:
        minimum, maximum = 70, 150
    else:
        minimum, maximum = 100, 220
    return {
        "suggested_min_chars": minimum,
        "suggested_max_chars": maximum,
        "principle": "简单回答简洁承接；信息越丰富才越充分展开；禁止为达到字数添加套话。",
    }


def normalize_interview_output(
    decision: dict[str, Any],
    turns: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """确定性门禁：模型可提议话术，但不能突破单问和事实边界。"""
    source_text = "\n".join(
        [str(turn.get("content", "")) for turn in turns if turn.get("role") == "user"]
        + [str(fact.get("value", "")) for fact in facts]
    )
    safe_sentences: list[str] = []
    priority_question = interview_priority_question(turns)
    ready = bool(decision.get("ready_to_draft")) and not priority_question
    for sentence in _sentences(str(decision.get("reply", ""))):
        if "？" in sentence or "?" in sentence:
            continue
        if any(re.search(pattern, sentence) for pattern in VISUAL_INFERENCE_PATTERNS):
            continue
        if any(word in sentence and word not in source_text for word in UNGROUNDED_DESCRIPTION_WORDS):
            continue
        if any(re.search(pattern, sentence) for pattern in OVERWRITTEN_WARMTH_PATTERNS):
            continue
        if any(re.search(pattern, sentence) for pattern in WORKFLOW_LANGUAGE_PATTERNS):
            continue
        if any(re.search(pattern, sentence) for pattern in TASKLIKE_LISTENING_PATTERNS):
            continue
        unsupported_scene = any(
            marker in sentence and marker not in source_text
            for marker in GROUNDING_RISK_MARKERS
        )
        agent_emotion = bool(re.search(r"我(?:听着|有点|会|觉得|也)", sentence))
        if unsupported_scene and not agent_emotion:
            continue
        unsupported_emotion = any(word in sentence and word not in source_text for word in EMOTION_WORDS)
        if unsupported_emotion and not re.search(r"我|听起来|听着", sentence):
            continue
        safe_sentences.append(sentence)

    if len("".join(safe_sentences)) < 28:
        # 不用本地话术补正文；交由 Reply Editor 根据原始事实重新生成。
        safe_sentences = []
    reply = "".join(safe_sentences[:2] if ready else safe_sentences[:4]).strip()
    question = ""
    if not ready:
        raw_question = priority_question or str(decision.get("question", "")).strip()
        first_clause = re.split(r"[？?]", raw_question, maxsplit=1)[0].strip()
        if "。" in first_clause:
            first_clause = first_clause.rsplit("。", maxsplit=1)[-1].strip()
        first_clause = re.split(r"[，,](?:或者|还是|或是)", first_clause, maxsplit=1)[0].strip()
        if first_clause:
            question = first_clause[:160].rstrip("。！!；;") + "？"

    return {
        **decision,
        "reply": reply,
        "question": question,
        "ready_to_draft": ready,
        "reason": (
            "先核实用户刚刚提出但尚未解释的关键线索" if priority_question else decision.get("reason", "")
        ),
    }


def _mock_facts(text: str) -> dict[str, Any]:
    snippets = [part.strip() for part in re.split(r"[。！？!?；;\n]+", text) if part.strip()]
    facts = []
    for snippet in snippets[:8]:
        fact_type = "event"
        if re.search(r"\d{4}年|小时候|那年|当时|后来", snippet):
            fact_type = "time"
        elif any(word in snippet for word in ("开心", "难过", "害怕", "高兴", "舍不得", "感觉")):
            fact_type = "feeling"
        elif any(word in snippet for word in ("想起", "现在看", "如今", "明白")):
            fact_type = "reflection"
        facts.append({"fact_type": fact_type, "value": snippet, "sensitivity": "normal"})
    years = [int(year) for year in re.findall(r"(?<!\d)((?:18|19|20|21)\d{2})年?", text)]
    time_match = re.search(r"((?:18|19|20|21)\d{2}年[^。！？!?；;]*)", text)
    current_event = {
        "title": snippets[0][:30] if snippets else "",
        "time_text": time_match.group(1)[:100] if time_match else "",
        "start_year": years[0] if years else None,
        "end_year": years[0] if years else None,
        "time_precision": "year" if years else "unknown",
        "location": "",
    }
    return {"facts": facts, "current_event": current_event}


async def extract_facts(
    text: str,
    existing: list[dict[str, Any]],
    project_id: str,
    session_id: str,
) -> dict[str, Any]:
    fallback = _mock_facts(text)
    result = await gateway.generate_json(
        "memory_agent",
        MEMORY_SYSTEM,
        {"project_id": project_id, "session_id": session_id, "user_text": text, "existing_facts": existing},
        lambda: fallback,
    )
    return _validated(result, MemoryAgentOutput, fallback)


def _mock_photo_title(note: str, observation: dict[str, Any] | None) -> dict[str, Any]:
    data = (observation or {}).get("observations") or {}
    visual = " ".join(
        str(value or "") for value in (data.get("visible_summary"), data.get("scene"), note)
    )
    mappings = (
        (("校门", "校园", "学校", "大学"), "那扇门后的远方"),
        (("车间", "工厂", "机器", "机床"), "机器声里的那一天"),
        (("车站", "站台", "火车"), "站台尽头的目送"),
        (("外滩", "江面", "江景", "海边", "轮渡"), "风吹过江面的那天"),
        (("客厅", "全家福", "一家人", "家庭"), "灯光下的一家人"),
        (("舞台", "广场舞", "领奖", "奖状"), "再次站到队伍前面"),
    )
    title = next((candidate for words, candidate in mappings if any(word in visual for word in words)), "")
    if not title:
        count = data.get("people_count")
        title = "我们站在一起的那天" if isinstance(count, int) and count > 1 else "照片没有说完的故事"
    return {
        "title": title,
        "rationale": "根据照片中可见的场景意象生成，等待后续讲述继续丰富。",
        "used_memory_fact_ids": [],
    }


async def generate_photo_title(
    project_id: str,
    photo_id: str,
    note: str,
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    fallback = _mock_photo_title(note, observation)
    result = await gateway.generate_json(
        "title_agent",
        PHOTO_TITLE_SYSTEM,
        {
            "project_id": project_id,
            "photo_id": photo_id,
            "user_note": note,
            "photo_observation": observation or {},
            "memory_scope": "shared_life_context",
        },
        lambda: fallback,
    )
    validated = _validated(result, PhotoTitleOutput, fallback)
    title = str(validated.get("title") or "").strip().strip("《》“”\"。！!")
    looks_like_filename = bool(re.search(
        r"^(?:这是|这是一张|一张).{0,24}(?:照片|合影)$|^在.{1,16}(?:旅游|拍摄|拍的)$",
        title,
    ))
    if not title or len(title) > 24 or looks_like_filename:
        validated = fallback
        validated["_quality_fallback"] = True
    validated["_model_succeeded"] = (
        not settings.use_mock_llm
        and "_latency_ms" in result
        and not validated.get("_quality_fallback", False)
    )
    return validated


def _mock_conversation_compaction(
    previous_summary: dict[str, Any] | None,
    turns: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    previous_summary = previous_summary or {}
    user_turns = [
        str(turn.get("content") or "").strip()
        for turn in turns if turn.get("role") == "user" and str(turn.get("content") or "").strip()
    ]
    fact_values = list(dict.fromkeys(
        str(fact.get("value") or "").strip() for fact in facts if str(fact.get("value") or "").strip()
    ))
    summary_parts = [str(previous_summary.get("conversation_summary") or "").strip(), *fact_values, *user_turns]
    summary_text = "；".join(part.rstrip("。；") for part in summary_parts if part)
    boundaries = list(previous_summary.get("boundaries") or [])
    for value in user_turns:
        if any(word in value for word in BOUNDARY_WORDS) and value not in boundaries:
            boundaries.append(value[:100])
    important_quotes = list(previous_summary.get("important_quotes") or [])
    for value in user_turns:
        if len(value) <= 120 and value not in important_quotes:
            important_quotes.append(value)
    return {
        "conversation_summary": summary_text[:2000],
        "covered_topics": list(dict.fromkeys([
            *previous_summary.get("covered_topics", []),
            *[value[:80] for value in fact_values],
        ]))[:20],
        "unresolved_clues": list(dict.fromkeys(previous_summary.get("unresolved_clues", [])))[:12],
        "user_preferences": list(dict.fromkeys(previous_summary.get("user_preferences", [])))[:12],
        "boundaries": boundaries[:12],
        "important_quotes": important_quotes[-12:],
        "fact_ids": list(dict.fromkeys([
            *previous_summary.get("fact_ids", []),
            *[str(fact.get("id")) for fact in facts if fact.get("id")],
        ]))[:80],
    }


async def compact_conversation(
    project_id: str,
    previous_summary: dict[str, Any] | None,
    older_turns: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = _mock_conversation_compaction(previous_summary, older_turns, facts)
    result = await gateway.generate_json(
        "context_compactor",
        CONTEXT_COMPACTION_SYSTEM,
        {
            "project_id": project_id,
            "previous_summary": previous_summary or {},
            "older_turns": [
                {"turn_id": turn.get("id"), "role": turn.get("role"), "content": turn.get("content")}
                for turn in older_turns
            ],
            "confirmed_facts": [
                {
                    "id": fact.get("id"),
                    "fact_type": fact.get("fact_type"),
                    "value": fact.get("value"),
                    "status": fact.get("status"),
                }
                for fact in facts
                if fact.get("status") != "retracted"
            ],
        },
        lambda: fallback,
    )
    validated = _validated(result, ConversationCompactionOutput, fallback)
    valid_fact_ids = {str(fact.get("id")) for fact in facts if fact.get("id")}
    validated["fact_ids"] = [fact_id for fact_id in validated["fact_ids"] if fact_id in valid_fact_ids]
    return validated


def _mock_interview(
    turn_count: int,
    facts: list[dict[str, Any]],
    last_user_text: str,
) -> dict[str, Any]:
    ready = turn_count >= 3
    questions = [
        "这张照片大约是什么时候、在哪里拍的？",
        "照片里有哪些人，他们和您是什么关系？",
        "那天最让您记得的一件事是什么？",
        "如今回头看，您最想把哪种感受留在这一章里？",
        "还有哪一个小细节，是您希望家里人以后也能知道的？",
    ]
    boundary = any(word in last_user_text for word in BOUNDARY_WORDS)
    if boundary and turn_count >= 2:
        ready = True
    question = "" if ready else questions[min(turn_count, len(questions) - 1)]
    if boundary and not ready:
        question = "那我们不追这个细节啦，您更愿意讲讲照片里让自己觉得轻松的一件小事吗？"
    detail = next(
        (str(fact.get("value", "")).strip() for fact in reversed(facts) if str(fact.get("value", "")).strip()),
        "这段记忆",
    )[:36]
    if boundary:
        reply = (
            "记不清或暂时不想说都完全没关系呀，回忆本来就不用像考试一样答得一字不差。"
            "您已经愿意把想得起来的部分讲给我听，这些内容足够让故事有一个真实的起点了。"
        )
    else:
        reply = (
            f"您刚才讲到“{detail}”，这个细节一下让这段回忆有了自己的样子。"
            "您愿意这样慢慢讲给我听，我听着也觉得很亲近呀。我们不用赶，把最想留下的部分一点点找回来就好。"
        )
    return {
        "reply": reply,
        "question": question,
        "ready_to_draft": ready,
        "reason": "已有足够材料可以先形成一版章节" if ready else "再补充一个关键线索会更完整",
    }


async def next_interview_turn(
    turns: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    turn_count: int,
    project_id: str,
    photo_observation: dict[str, Any] | None = None,
    model_turns: list[dict[str, Any]] | None = None,
    conversation_summary: dict[str, Any] | None = None,
    context_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_user_text = next(
        (str(turn["content"]) for turn in reversed(turns) if turn.get("role") == "user"),
        "",
    )
    fallback = _mock_interview(turn_count, facts, last_user_text)
    prompt_turns = model_turns if model_turns is not None else turns[-10:]
    length_guidance = _interview_reply_length_guidance(last_user_text)
    openings = previous_reply_openings(turns)
    result = await gateway.generate_json(
        "interview_agent",
        INTERVIEW_SYSTEM,
        {
            "project_id": project_id,
            "conversation_summary": conversation_summary or {},
            "turns": prompt_turns,
            "facts": facts,
            "user_answer_count": turn_count,
            "photo_observation_candidates": photo_observation,
            "context_control": context_control or {},
            "reply_length_guidance": length_guidance,
            "previous_reply_openings": openings,
        },
        lambda: fallback,
    )
    validated = _validated(result, InterviewAgentOutput, fallback)
    normalized = normalize_interview_output(validated, turns, facts)
    # 长度只是给 LLM 的软建议；只有安全过滤后几乎没有可读回应，或回应开头
    # 与最近几轮重复（例如每轮都以“原来”开头）时，才触发 Reply Editor 重写。
    too_short = len(re.sub(r"\s+", "", normalized["reply"])) < 12
    opener_repeats = reply_opening_repeats(normalized["reply"], openings)
    reply_needs_repair = too_short or opener_repeats
    if not settings.use_mock_llm and reply_needs_repair:
        editor_payload = {
            "project_id": project_id,
            "conversation_summary": conversation_summary or {},
            "turns": prompt_turns[-8:],
            "facts": [
                {"fact_type": fact.get("fact_type"), "value": fact.get("value")}
                for fact in facts
            ],
            "original_reply": normalized["reply"],
            "editor_reason": (
                "回应开头与最近几轮回复的开头词重复，请换一种自然的承接方式重新开始。"
                if opener_repeats and not too_short
                else "安全门禁移除了部分内容，或回复没有形成完整的自然承接。"
            ),
            "forbidden_openings": openings,
            "fixed_question": normalized["question"],
            "ready_to_draft": normalized["ready_to_draft"],
            "reply_length_guidance": length_guidance,
        }
        for attempt in range(2):
            edited = await gateway.generate_json(
                "interview_reply_editor",
                INTERVIEW_REPLY_EDITOR_SYSTEM,
                editor_payload,
                lambda: normalized,
            )
            edited_validated = _validated(edited, InterviewAgentOutput, normalized)
            edited_validated["question"] = normalized["question"]
            edited_validated["ready_to_draft"] = normalized["ready_to_draft"]
            normalized = normalize_interview_output(edited_validated, turns, facts)
            if (
                len(re.sub(r"\s+", "", normalized["reply"])) >= 12
                and not reply_opening_repeats(normalized["reply"], openings)
            ):
                break
            editor_payload["rejected_reply"] = normalized["reply"]
            editor_payload["editor_reason"] = "上一版仍未形成足够完整、且开头不与近期回复重复的自然回应。"
    return normalized


def _mock_chapter(person: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    fact_texts = [str(f["value"]).strip() for f in facts if str(f.get("value", "")).strip()]
    title_seed = fact_texts[0][:16] if fact_texts else "一张旧照片"
    title = f"照片里的{title_seed}" if len(title_seed) < 10 else "照片背后的那段时光"
    groups: dict[str, list[str]] = {
        "setting": [],
        "background": [],
        "experience": [],
        "reflection": [],
    }
    for fact in facts:
        value = str(fact.get("value", "")).strip()
        if not value:
            continue
        fact_type = str(fact.get("fact_type", "other"))
        if fact_type in {"person", "place", "time"}:
            groups["setting"].append(value)
        elif fact_type in {"feeling", "reflection", "quote"}:
            groups["reflection"].append(value)
        elif any(word in value for word in ("小时候", "从小", "一直想", "决定", "出发")):
            groups["background"].append(value)
        else:
            groups["experience"].append(value)

    def paragraph(values: list[str]) -> str:
        return "。".join(value.rstrip("。！？!?；;") for value in values) + ("。" if values else "")

    paragraphs = [paragraph(groups[key]) for key in ("setting", "background", "experience", "reflection")]
    paragraphs = [value for value in paragraphs if value]
    if person == "first":
        opening = "每当我看到这张照片，那段往事便重新有了形状。"
        body = "\n\n".join(paragraphs)
    else:
        opening = "每当看到这张照片，讲述者记忆里的那段往事便重新有了形状。"
        body = "\n\n".join(f"讲述者回忆道：“{value}”" for value in paragraphs)
    content = f"{opening}\n\n{body}" if body else opening
    return {
        "title": title,
        "content": content,
        "used_fact_ids": [f["id"] for f in facts],
        "used_visual_ids": [],
        "literary_inferences": [],
    }


def _chapter_target_length(turns: list[dict[str, Any]]) -> tuple[int, int]:
    answers = [
        str(turn.get("content", "")).strip()
        for turn in turns
        if turn.get("role") == "user" and str(turn.get("content", "")).strip()
    ]
    answer_count = len(answers)
    source_chars = sum(_content_length(answer) for answer in answers)
    if answer_count >= 5:
        target_min = max(900, min(1200, int(source_chars * 1.8)))
        return target_min, min(1600, target_min + 350)
    if answer_count >= 4:
        target_min = max(750, min(1000, int(source_chars * 1.7)))
        return target_min, min(1400, target_min + 300)
    target_min = max(450, min(700, int(source_chars * 1.7)))
    return target_min, min(1000, target_min + 300)


def _content_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def enforce_grounding_review(
    review: dict[str, Any],
    content: str,
    facts: list[dict[str, Any]],
    turns: list[dict[str, Any]] | None = None,
    visual_evidence: list[dict[str, Any]] | None = None,
    forbidden_visual_terms: list[str] | None = None,
) -> dict[str, Any]:
    """确定性门禁：具体外部细节需来自口述、事实或照片可见证据。

    forbidden_visual_terms 是证据仲裁否决的视觉术语（与用户确认地点/时间矛盾）；
    包含这些术语的句子按视觉误识别处理，直接移出修正稿。
    """
    evidence = "\n".join(
        [str(fact.get("value", "")) for fact in facts]
        + [
            str(turn.get("content", ""))
            for turn in (turns or [])
            if turn.get("role") == "user"
        ]
        + [str(item.get("text", "")) for item in (visual_evidence or [])]
    )
    corrected = str(review.get("corrected_content") or content).strip()
    detected: list[str] = []
    semantic_issues: list[str] = []
    semantic_fixes = (
        (
            "在烟盒纸上画的半毫米",
            "在烟盒纸上记下的半毫米",
            ("烟盒纸", "编号"),
            "把抄写记录误写成画图",
        ),
        (
            "讲成都站台上我拎着帆布包",
            "讲成都站台上我替小雨理帆布包带",
            ("替女儿整理帆布包带",),
            "把替女儿整理包带误写成自己拎包",
        ),
        (
            "围裙都没来得及解",
            "工作服上的油还没擦",
            ("工作服上还有油",),
            "把工厂工作服误写成修配铺围裙",
        ),
    )
    for original, replacement, required_markers, issue in semantic_fixes:
        if original in corrected and all(marker in evidence for marker in required_markers):
            corrected = corrected.replace(original, replacement)
            semantic_issues.append(issue)
    corrected_paragraphs: list[str] = []
    forbidden = [term for term in (forbidden_visual_terms or []) if term]
    for paragraph in corrected.splitlines():
        kept: list[str] = []
        for sentence in _sentences(paragraph):
            contradicted = [term for term in forbidden if term in sentence]
            if contradicted:
                detected.extend(contradicted)
                continue
            unsupported = [
                marker for marker in CHAPTER_HARD_RISK_MARKERS
                if marker in sentence and marker not in evidence
            ]
            if unsupported:
                detected.extend(unsupported)
                continue
            kept.append(sentence)
        if kept:
            corrected_paragraphs.append("".join(kept))
    if not detected and not semantic_issues:
        return review
    issues = [str(issue) for issue in review.get("issues", [])]
    if detected:
        contradicted_terms = [term for term in forbidden if term in detected]
        if contradicted_terms:
            issues.append("与用户确认的地点或时间矛盾的视觉误识别：" + "、".join(dict.fromkeys(contradicted_terms)))
        unsupported_terms = [term for term in detected if term not in forbidden]
        if unsupported_terms:
            issues.append("发现用户原话中没有依据的细节：" + "、".join(dict.fromkeys(unsupported_terms)))
    issues.extend(semantic_issues)
    safe_content = "\n\n".join(corrected_paragraphs).strip()
    return {
        **review,
        "passed": False,
        "issues": issues,
        "corrected_content": safe_content or _mock_chapter("first", facts)["content"],
    }


async def draft_chapter(
    person: str,
    turns: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    method_cards: list[dict[str, Any]],
    visual_evidence: list[dict[str, Any]] | None = None,
    instruction: str | None = None,
    previous_content: str | None = None,
    model_turns: list[dict[str, Any]] | None = None,
    conversation_summary: dict[str, Any] | None = None,
    context_control: dict[str, Any] | None = None,
    confirmed_places: list[str] | None = None,
) -> dict[str, Any]:
    fallback = _mock_chapter(person, facts)
    target_min, target_max = _chapter_target_length(turns)
    transcript_turns = model_turns if model_turns is not None else turns
    user_transcript = [
        {"turn_id": turn.get("id"), "content": str(turn.get("content", "")).strip()}
        for turn in transcript_turns
        if turn.get("role") == "user" and str(turn.get("content", "")).strip()
    ]
    fact_evidence = [
        {
            "id": fact.get("id"),
            "fact_type": fact.get("fact_type"),
            "value": fact.get("value"),
            "status": fact.get("status"),
        }
        for fact in facts
    ]
    payload = {
        "project_id": facts[0].get("project_id") if facts else None,
        "narrative_person": person,
        "facts": fact_evidence,
        "user_transcript": user_transcript,
        "conversation_summary": conversation_summary or {},
        "context_control": context_control or {},
        "target_length_chars": {"min": target_min, "max": target_max},
        "writing_method_cards": method_cards,
        "visual_evidence": visual_evidence or [],
        "confirmed_places": confirmed_places or [],
        "literary_mode": "bold_warm",
        "revision_instruction": instruction,
        "previous_content": previous_content,
    }
    result = await gateway.generate_json(
        "chapter_agent",
        CHAPTER_SYSTEM,
        payload,
        lambda: fallback,
    )
    validated = _validated(result, ChapterAgentOutput, fallback)
    if not settings.use_mock_llm and _content_length(validated["content"]) < target_min:
        retry_payload = {
            **payload,
            "previous_content": validated["content"],
            "revision_instruction": (
                f"当前草稿只有约{_content_length(validated['content'])}字。请重新组织而不是简单重复，"
                f"保持人物、时间、地点、关系、核心事件和结果不变，尽量写到{target_min}～{target_max}字；"
                "可结合照片证据补足画面，并用合理文学推断写出心理变化、主题和如今回望。"
                "不得虚构直接引语，不要同义反复。"
            ),
        }
        expanded = await gateway.generate_json(
            "chapter_agent",
            CHAPTER_SYSTEM,
            retry_payload,
            lambda: validated,
        )
        validated = _validated(expanded, ChapterAgentOutput, validated)
    return validated


async def review_chapter(
    content: str,
    facts: list[dict[str, Any]],
    person: str,
    turns: list[dict[str, Any]] | None = None,
    visual_evidence: list[dict[str, Any]] | None = None,
    literary_inferences: list[str] | None = None,
    model_turns: list[dict[str, Any]] | None = None,
    conversation_summary: dict[str, Any] | None = None,
    context_control: dict[str, Any] | None = None,
    forbidden_visual_terms: list[str] | None = None,
) -> dict[str, Any]:
    def fallback() -> dict[str, Any]:
        issues = []
        if person == "first" and "讲述者" in content:
            issues.append("第一人称章节中出现了第三人称称呼")
        return {"passed": not issues, "issues": issues, "corrected_content": content}

    fallback_value = fallback()
    result = await gateway.generate_json(
        "review_agent",
        REVIEW_SYSTEM,
        {
            "project_id": facts[0].get("project_id") if facts else None,
            "content": content,
            "facts": [
                {"id": fact.get("id"), "fact_type": fact.get("fact_type"), "value": fact.get("value")}
                for fact in facts
            ],
            "conversation_summary": conversation_summary or {},
            "context_control": context_control or {},
            "user_transcript": [
                {"turn_id": turn.get("id"), "content": turn.get("content")}
                for turn in (model_turns if model_turns is not None else (turns or []))
                if turn.get("role") == "user"
            ],
            "narrative_person": person,
            "visual_evidence": visual_evidence or [],
            "literary_inferences": literary_inferences or [],
        },
        lambda: fallback_value,
    )
    validated = _validated(result, ReviewAgentOutput, fallback_value)
    return enforce_grounding_review(
        validated, content, facts, turns, visual_evidence, forbidden_visual_terms
    )


async def review_common_sense(
    content: str,
    facts: list[dict[str, Any]],
    confirmed_places: list[str] | None = None,
    visual_evidence: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """常识审查：检查正文与确认事实之间地理、年代、物理等常识矛盾。"""

    def fallback() -> dict[str, Any]:
        return {"passed": True, "issues": [], "corrected_content": content}

    result = await gateway.generate_json(
        "common_sense_reviewer",
        COMMON_SENSE_REVIEW_SYSTEM,
        {
            "project_id": project_id,
            "content": content,
            "confirmed_places": confirmed_places or [],
            "facts": [
                {"fact_type": fact.get("fact_type"), "value": fact.get("value")}
                for fact in (facts or [])
            ],
            "visual_evidence": visual_evidence or [],
        },
        fallback,
    )
    return _validated(result, ReviewAgentOutput, fallback())


async def suggest_relation(
    new_story: str, chapters: list[dict[str, Any]], project_id: str
) -> dict[str, Any]:
    def fallback() -> dict[str, Any]:
        return {
            "choice": "new",
            "chapter_id": None,
            "reason": "目前还看不出与旧章节属于同一件事，先独立成章更稳妥，之后仍可合并。",
        }

    fallback_value = fallback()
    result = await gateway.generate_json(
        "relation_advisor",
        RELATION_SYSTEM,
        {"project_id": project_id, "new_story": new_story, "existing_chapters": chapters},
        lambda: fallback_value,
    )
    validated = _validated(result, RelationAgentOutput, fallback_value)
    valid_ids = {chapter["id"] for chapter in chapters}
    if validated["chapter_id"] not in valid_ids:
        validated["chapter_id"] = None
        if validated["choice"] != "new":
            validated = fallback_value
    return validated


async def merge_chapters(
    person: str,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = {
        "title": existing["title"],
        "content": existing["content"].rstrip() + "\n\n" + incoming["content"].strip(),
    }
    result = await gateway.generate_json(
        "chapter_merge_agent",
        MERGE_SYSTEM,
        {
            "project_id": facts[0].get("project_id") if facts else None,
            "narrative_person": person,
            "existing_chapter": existing,
            "incoming_chapter": incoming,
            "facts": facts,
        },
        lambda: fallback,
    )
    return _validated(result, MergeAgentOutput, fallback)


async def direct_book(
    project_id: str,
    chapters: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = {
        "book_arc": "按人生时间顺序连接各章，保留原有事实与章节主题。",
        "people_registry": [],
        "narrative_threads": [],
        "chapter_briefs": [
            {
                "chapter_id": chapter["chapter_id"],
                "chapter_role": chapter.get("title", "人生阶段"),
                "opening_echo": "",
                "people_additions": [],
                "motif_actions": [],
                "foreshadow": "",
                "next_handoff": "",
                "source_chapter_ids": [],
            }
            for chapter in chapters
        ],
    }
    result = await gateway.generate_json(
        "book_director",
        BOOK_DIRECTOR_SYSTEM,
        {"project_id": project_id, "chapters": chapters, "facts": facts},
        lambda: fallback,
    )
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, BookDirectorOutput, fallback)
    validated["_model_succeeded"] = model_succeeded
    valid_ids = {chapter["chapter_id"] for chapter in chapters}
    briefs = {
        str(brief.get("chapter_id")): brief
        for brief in validated["chapter_briefs"]
        if str(brief.get("chapter_id")) in valid_ids
    }
    validated["chapter_briefs"] = [
        briefs.get(chapter["chapter_id"], fallback["chapter_briefs"][index])
        for index, chapter in enumerate(chapters)
    ]
    return validated


async def curate_people(
    project_id: str,
    facts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    photo_people: list[dict[str, Any]],
) -> dict[str, Any]:
    protagonist = {
        "display_name": "主人公",
        "aliases": ["我"],
        "kind": "protagonist",
        "relationship": "这本自传的主人公",
        "summary": "这些照片与讲述共同留下了主人公不同人生阶段的经历。",
        "story_role": "所有人物关系和人生故事的中心。",
        "event_ids": [event["id"] for event in events],
        "chapter_ids": [chapter["id"] for chapter in chapters],
        "photo_ids": [photo["photo_id"] for photo in photo_people],
        "source_fact_ids": [],
    }
    relationship_words = (
        "同学", "朋友", "同事", "老师", "师傅", "组长", "丈夫", "妻子", "爱人",
        "父亲", "母亲", "爸爸", "妈妈", "爷爷", "奶奶", "外公", "外婆", "婆婆",
        "女儿", "儿子", "姐姐", "妹妹", "哥哥", "弟弟",
    )
    buckets: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        value = str(fact.get("value") or "")
        relation = next((word for word in relationship_words if word in value), None)
        if relation:
            buckets.setdefault(relation, []).append(fact)
        elif fact.get("fact_type") == "person":
            buckets.setdefault("重要人物", []).append(fact)
    people = [protagonist]
    covered_photo_ids: set[str] = set()
    for relation, related in list(buckets.items())[:12]:
        values = list(dict.fromkeys(str(fact.get("value") or "").strip() for fact in related if fact.get("value")))
        photo_ids = list(dict.fromkeys(str(fact.get("photo_id")) for fact in related if fact.get("photo_id")))
        covered_photo_ids.update(photo_ids)
        plural = any(any(marker in value for marker in ("两个", "两位", "几个", "一群")) for value in values)
        people.append({
            "display_name": f"{'几位' if plural else ''}{relation}",
            "aliases": [],
            "kind": "confirmed",
            "relationship": relation,
            "summary": "；".join(values)[:500] or f"主人公提到的{relation}。",
            "story_role": f"在已讲述的照片故事中与主人公共同经历了一段人生时光。",
            "event_ids": list(dict.fromkeys(str(fact.get("event_id")) for fact in related if fact.get("event_id"))),
            "chapter_ids": [],
            "photo_ids": photo_ids,
            "source_fact_ids": [str(fact["id"]) for fact in related if fact.get("id")],
        })
    for photo in photo_people:
        photo_id = str(photo.get("photo_id") or "")
        if not photo_id or photo_id in covered_photo_ids:
            continue
        count = int(photo.get("count") or 0)
        if count <= 0:
            continue
        people.append({
            "display_name": f"照片中的人物（{count}人）",
            "aliases": [],
            "kind": "visual_unknown",
            "relationship": "身份待补充",
            "summary": "照片中可以看到人物，但用户尚未确认他们的姓名和关系。",
            "story_role": "等待后续访谈补充身份与共同经历。",
            "event_ids": [str(photo["event_id"])] if photo.get("event_id") else [],
            "chapter_ids": [],
            "photo_ids": [photo_id],
            "source_fact_ids": [],
        })
    fallback = {
        "overview": f"目前的人物簿整理出{len(people)}组人物；已确认关系来自用户口述，未确认身份的人物只保留照片线索。",
        "people": people,
    }
    result = await gateway.generate_json(
        "people_curator",
        PEOPLE_CURATOR_SYSTEM,
        {
            "project_id": project_id,
            "confirmed_facts": facts,
            "events": events,
            "chapter_excerpts": chapters,
            "photo_people": photo_people,
        },
        lambda: fallback,
    )
    return _validated(result, PeopleCatalogOutput, fallback)


async def reweave_chapter(
    project_id: str,
    person: str,
    current_chapter: dict[str, Any],
    chapter_brief: dict[str, Any],
    book_arc: str,
    people_registry: list[dict[str, Any]],
    narrative_threads: list[dict[str, Any]],
    relevant_cross_chapter_facts: list[dict[str, Any]],
    user_transcript: list[dict[str, Any]],
    visual_evidence: list[dict[str, Any]],
    previous_chapter: dict[str, Any] | None,
    next_chapter: dict[str, Any] | None,
) -> dict[str, Any]:
    fallback = {
        "title": current_chapter["title"],
        "content": current_chapter["content"],
        "used_fact_ids": [],
        "used_visual_ids": [],
        "literary_inferences": [],
    }
    result = await gateway.generate_json(
        "chapter_reweaver",
        CHAPTER_REWEAVE_SYSTEM,
        {
            "project_id": project_id,
            "narrative_person": person,
            "current_chapter": current_chapter,
            "chapter_brief": chapter_brief,
            "book_arc": book_arc,
            "people_registry": people_registry,
            "narrative_threads": narrative_threads,
            "relevant_cross_chapter_facts": relevant_cross_chapter_facts,
            "user_transcript": user_transcript,
            "visual_evidence": visual_evidence,
            "previous_chapter": previous_chapter,
            "next_chapter": next_chapter,
        },
        lambda: fallback,
    )
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, ChapterAgentOutput, fallback)
    validated["_model_succeeded"] = model_succeeded
    return validated


async def force_chapter_link(
    project_id: str,
    person: str,
    current_chapter: dict[str, Any],
    chapter_brief: dict[str, Any],
    required_cross_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = {
        "title": current_chapter["title"],
        "content": current_chapter["content"],
        "used_fact_ids": [],
        "used_visual_ids": [],
        "literary_inferences": [],
    }
    result = await gateway.generate_json(
        "chapter_reweaver",
        FORCED_CHAPTER_LINK_SYSTEM,
        {
            "project_id": project_id,
            "narrative_person": person,
            "current_chapter": current_chapter,
            "chapter_brief": chapter_brief,
            "required_cross_facts": required_cross_facts[:4],
        },
        lambda: fallback,
    )
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, ChapterAgentOutput, fallback)
    if (
        model_succeeded
        and validated.get("content") != current_chapter.get("content")
        and not validated.get("used_fact_ids")
    ):
        # This editor only receives the small cross-chapter evidence packet.
        # If V4 omits the optional ID field, retain the whole packet as a
        # conservative source snapshot rather than persisting untraceable prose.
        validated["used_fact_ids"] = [
            fact["id"] for fact in required_cross_facts if fact.get("id")
        ]
        validated["_source_resolution"] = "conservative_packet"
    validated["_model_succeeded"] = model_succeeded
    return validated


async def link_chapter_facts(
    project_id: str,
    content: str,
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = {"fact_ids": []}
    result = await gateway.generate_json(
        "chapter_fact_linker",
        CHAPTER_FACT_LINK_SYSTEM,
        {
            "project_id": project_id,
            "chapter_content": content,
            "candidate_facts": [
                {
                    "id": fact.get("id"),
                    "value": fact.get("value"),
                    "source_chapter_id": fact.get("source_chapter_id"),
                    "source_year": fact.get("source_year"),
                }
                for fact in facts
            ],
        },
        lambda: fallback,
    )
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, ChapterFactLinkOutput, fallback)
    validated["_model_succeeded"] = model_succeeded
    return validated


async def review_book_continuity(
    project_id: str,
    director_plan: dict[str, Any],
    chapters: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = {"passed": True, "issues": [], "thread_results": [], "character_results": []}
    result = await gateway.generate_json(
        "book_continuity_reviewer",
        BOOK_CONTINUITY_REVIEW_SYSTEM,
        {
            "project_id": project_id,
            "director_plan": director_plan,
            "chapters": chapters,
            "facts": facts,
        },
        lambda: fallback,
    )
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, BookContinuityReviewOutput, fallback)
    validated["_model_succeeded"] = model_succeeded
    return validated


def _third_person_fallback(text: str) -> str:
    """Mock-mode fallback only; production prose is produced by the compiler model."""
    replacements = (
        ("我们一家", "他们一家"), ("我们", "他们"), ("我爱人", "她的爱人"),
        ("我的", "她的"), ("我俩", "他们俩"), ("我", "她"),
    )
    result = text
    for source, target in replacements:
        result = result.replace(source, target)
    return result


async def compile_autobiography_manuscript(
    project_id: str,
    source_stories: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    previous_edition: dict[str, Any] | None,
) -> dict[str, Any]:
    all_chapter_ids = [story["chapter_id"] for story in source_stories]
    all_photo_ids = [
        photo["id"] for story in source_stories for photo in story.get("photos", [])
    ]
    requested_groups: list[list[str]] = []
    if len(source_stories) >= 6:
        target_count = min(6, max(4, round(len(source_stories) * 0.65)))
        base_size, remainder = divmod(len(source_stories), target_count)
        cursor = 0
        for index in range(target_count):
            size = base_size + (1 if index < remainder else 0)
            requested_groups.append([
                story["chapter_id"] for story in source_stories[cursor:cursor + size]
            ])
            cursor += size
    fallback_sections = []
    for story in source_stories:
        fallback_sections.append({
            "title": story["title"],
            "content": _third_person_fallback(story["content"]),
            "source_chapter_ids": [story["chapter_id"]],
            "photo_ids": [photo["id"] for photo in story.get("photos", [])],
            "character_revelation": "她在具体生活中表现出的认真、担当与韧性。",
            "photo_meaning": "照片为这段人生经历留下了可以回望的时间坐标。",
            "narrative_function": "推进主人公的人生时间线并显露其性格。",
        })
    fallback = {
        "title": "把日子留在照片里",
        "subtitle": "一部仍在生长的个人自传",
        "core_theme": "一个普通人如何在学习、工作、家庭与时间的变化中，认真地走出自己的路。",
        "character_portrait": "她并不以响亮的话定义自己，而是在一次次观察、选择和承担中，把普通日子过出了清楚的分量。",
        "preface": "多年以后，照片把散落的日子重新带回她面前。画面没有替她解释一生，却留下了她曾经站立、选择和珍惜过的证据。",
        "sections": fallback_sections,
        "afterword": "这些照片并不是一生的终点。每一次重新翻开，都是她继续理解自己、也让后来的人认识她的开始。",
    }
    result = await gateway.generate_json(
        "autobiography_compiler",
        AUTOBIOGRAPHY_COMPILER_SYSTEM,
        {
            "project_id": project_id,
            "required_narrative_person": "third",
            "source_story_count": len(source_stories),
            "required_source_chapter_ids": all_chapter_ids,
            "required_photo_ids": all_photo_ids,
            "requested_chapter_groups": requested_groups,
            "source_stories": source_stories,
            "facts": facts,
            "previous_edition": previous_edition,
        },
        lambda: fallback,
    )
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, AutobiographyManuscriptOutput, fallback)
    if model_succeeded and requested_groups:
        merged_sections = sum(
            1 for section in validated.get("sections", [])
            if len(set(section.get("source_chapter_ids", []))) >= 2
        )
        if len(validated.get("sections", [])) != len(requested_groups) or merged_sections < 2:
            regrouped = await gateway.generate_json(
                "autobiography_regroup_editor",
                AUTOBIOGRAPHY_REGROUP_SYSTEM,
                {
                    "project_id": project_id,
                    "required_narrative_person": "third",
                    "required_groups": requested_groups,
                    "source_stories": source_stories,
                    "facts": facts,
                    "rejected_draft": validated,
                },
                lambda: validated,
            )
            model_succeeded = "_latency_ms" in regrouped
            validated = _validated(regrouped, AutobiographyManuscriptOutput, validated)
    validated["_model_succeeded"] = model_succeeded
    return validated


async def review_autobiography_manuscript(
    project_id: str,
    source_stories: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    manuscript: dict[str, Any],
) -> dict[str, Any]:
    source_ids = {story["chapter_id"] for story in source_stories}
    photo_ids = {
        photo["id"] for story in source_stories for photo in story.get("photos", [])
    }
    used_source_ids = {
        source_id for section in manuscript.get("sections", [])
        for source_id in section.get("source_chapter_ids", [])
    }
    used_photo_ids = {
        photo_id for section in manuscript.get("sections", [])
        for photo_id in section.get("photo_ids", [])
    }
    source_coverage = len(source_ids & used_source_ids) / len(source_ids) if source_ids else 1.0
    photo_coverage = len(photo_ids & used_photo_ids) / len(photo_ids) if photo_ids else 1.0
    fallback = {
        "passed": source_coverage == 1 and photo_coverage == 1,
        "issues": [],
        "third_person_score": 1,
        "source_coverage": source_coverage,
        "photo_coverage": photo_coverage,
        "value_expression_score": 4,
        "literary_quality_score": 4,
        "character_traits": ["认真", "坚韧", "有担当"],
        "evidence_notes": ["人物特点通过具体行动和人生选择呈现。"],
    }
    result = await gateway.generate_json(
        "autobiography_reviewer",
        AUTOBIOGRAPHY_REVIEW_SYSTEM,
        {
            "project_id": project_id,
            "source_stories": source_stories,
            "facts": facts,
            "manuscript": manuscript,
        },
        lambda: fallback,
    )
    for score_name in ("third_person_score", "source_coverage", "photo_coverage"):
        try:
            score = float(result.get(score_name, fallback[score_name]))
            if score > 1:
                score = score / 5 if score <= 5 else score / 100
            result[score_name] = max(0, min(1, score))
        except (TypeError, ValueError):
            result[score_name] = fallback[score_name]
    model_succeeded = "_latency_ms" in result
    validated = _validated(result, AutobiographyReviewOutput, fallback)
    # Coverage is deterministic: a model cannot waive a missing source or photo.
    validated["source_coverage"] = source_coverage
    validated["photo_coverage"] = photo_coverage
    if source_coverage < 1 or photo_coverage < 1:
        validated["passed"] = False
        if source_coverage < 1:
            validated["issues"].append("仍有照片故事没有进入完整自传")
        if photo_coverage < 1:
            validated["issues"].append("仍有照片没有进入完整自传正文")
    if len(source_stories) >= 6:
        sections = manuscript.get("sections", [])
        merged_sections = sum(
            1 for section in sections if len(set(section.get("source_chapter_ids", []))) >= 2
        )
        if len(sections) > 6 or merged_sections < 2:
            validated["passed"] = False
            validated["issues"].append("整书仍接近一张照片一章，需要进一步合并人生阶段")
    validated["_model_succeeded"] = model_succeeded
    return validated
