from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import settings
from app.context_memory import estimate_tokens
from app.agents import (
    _chapter_target_length,
    _interview_reply_length_guidance,
    enforce_grounding_review,
    interview_priority_question,
    normalize_interview_output,
)
from app.db import execute, fetch_all
from app.main import app
from app.vision import opening_from_observation


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
)


def create_project(client: TestClient, title: str = "核心功能测试", person: str = "first") -> dict[str, Any]:
    response = client.post("/api/projects", json={"title": title, "narrative_person": person})
    assert response.status_code == 200
    return response.json()


def upload_story(
    client: TestClient,
    project_id: str,
    answers: list[str],
    filename: str = "memory.png",
) -> tuple[dict[str, Any], dict[str, Any]]:
    uploaded = client.post(
        f"/api/projects/{project_id}/photos",
        files={"image": (filename, PNG_BYTES + filename.encode(), "image/png")},
        data={"note": "测试照片"},
    )
    assert uploaded.status_code == 200
    result = uploaded.json()
    session = result["session"]
    for answer in answers:
        response = client.post(f"/api/sessions/{session['id']}/reply", json={"text": answer})
        assert response.status_code == 200
        session = response.json()
    return result["photo"], session


def generate(client: TestClient, session_id: str) -> dict[str, Any]:
    response = client.post(f"/api/sessions/{session_id}/generate")
    assert response.status_code == 200, response.text
    return response.json()


def test_project_perspective_and_health() -> None:
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health == {
            "status": "ok",
            "llm_mode": "mock",
            "model": settings.deepseek_model,
            "voice_enabled": False,
            "vision_enabled": False,
            "vision_mode": "disabled",
            "vision_model": None,
        }
        project = create_project(client, person="first")
        updated = client.patch(
            f"/api/projects/{project['id']}", json={"narrative_person": "third"}
        )
        assert updated.status_code == 200
        assert updated.json()["narrative_person"] == "third"
        client.delete(f"/api/projects/{project['id']}")


def test_third_person_is_used_by_new_chapter() -> None:
    with TestClient(app) as client:
        project = create_project(client, "第三人称测试", person="third")
        _, session = upload_story(client, project["id"], ["我那年刚参加工作，心里很踏实。"])
        chapter = generate(client, session["id"])
        assert chapter["current_version"]["narrative_person"] == "third"
        assert "讲述者" in chapter["current_version"]["content"]
        client.delete(f"/api/projects/{project['id']}")


def test_photo_validation_and_local_storage() -> None:
    with TestClient(app) as client:
        project = create_project(client, "照片验证")
        rejected = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("note.txt", b"not an image", "text/plain")},
        )
        assert rejected.status_code == 400
        disguised = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("fake.png", b"not really a png", "image/png")},
        )
        assert disguised.status_code == 400
        photo, _ = upload_story(client, project["id"], ["这是在老家拍的一张照片。"])
        observation = client.get(f"/api/photos/{photo['id']}/observation").json()
        assert observation["status"] == "metadata_only"
        media_path = settings.media_dir / photo["stored_name"]
        assert media_path.exists()
        client.delete(f"/api/projects/{project['id']}")
        assert not media_path.exists()


def test_shared_life_snapshot_is_injected_into_agent_context() -> None:
    with TestClient(app) as client:
        project = create_project(client, "周桂兰：照片自传", person="third")
        _, session = upload_story(
            client,
            project["id"],
            ["1985年我和爱人陈国强在井研县机械厂参加技术革新，我们后来一起站在车间门口拍了合影。"],
        )
        memory_context = client.get(f"/api/projects/{project['id']}/memory-context")
        assert memory_context.status_code == 200
        snapshot = memory_context.json()["life_context_snapshot"]
        assert snapshot["protagonist"]["name"] == "周桂兰"
        assert snapshot["important_events"]
        assert any("陈国强" in fact["value"] for fact in snapshot["key_facts"])

        runs = fetch_all(
            "SELECT input_json FROM model_runs WHERE project_id = ? AND agent_name = 'interview_agent' ORDER BY created_at DESC",
            (project["id"],),
        )
        assert runs
        agent_input = json.loads(runs[0]["input_json"])
        assert agent_input["shared_life_context"]["snapshot_id"]
        assert agent_input["_context_metadata"]["shared_snapshot_id"] == agent_input["shared_life_context"]["snapshot_id"]
        assert session["turns"]
        client.delete(f"/api/projects/{project['id']}")


def test_context_compression_triggers_at_token_boundary_without_deleting_turns() -> None:
    original_trigger = settings.context_compression_trigger_tokens
    original_target = settings.context_compression_target_tokens
    original_recent = settings.context_recent_turns
    object.__setattr__(settings, "context_compression_trigger_tokens", 700)
    object.__setattr__(settings, "context_compression_target_tokens", 360)
    object.__setattr__(settings, "context_recent_turns", 4)
    try:
        with TestClient(app) as client:
            project = create_project(client, "压缩机制测试")
            uploaded = client.post(
                f"/api/projects/{project['id']}/photos",
                files={"image": ("long-memory.png", PNG_BYTES + b"long", "image/png")},
                data={"note": "1985年机械厂合影"},
            ).json()
            session_id = uploaded["session"]["id"]
            long_answer = (
                "1985年我和爱人陈国强在井研县机械厂参加技术革新，照片是在车间门口拍的。"
                + "那段经历让我一直记得人与人一起做事的过程。" * 35
            )
            response = client.post(f"/api/sessions/{session_id}/reply", json={"text": long_answer})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["context_memory"]["triggered"] is True
            assert body["context_memory"]["compressed"] is True
            assert body["context_memory"]["compaction_version"] == 1

            original_turns = fetch_all(
                "SELECT * FROM interview_turns WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
            )
            compactions = fetch_all(
                "SELECT * FROM conversation_compactions WHERE session_id = ?", (session_id,)
            )
            assert len(original_turns) == 3
            assert len(compactions) == 1
            summary = json.loads(compactions[0]["summary_json"])
            assert "陈国强" in summary["conversation_summary"]
            assert compactions[0]["compressed_token_count"] < compactions[0]["source_token_count"]

            interview_runs = fetch_all(
                "SELECT input_json FROM model_runs WHERE project_id = ? AND agent_name = 'interview_agent' ORDER BY created_at DESC",
                (project["id"],),
            )
            model_input = json.loads(interview_runs[0]["input_json"])
            assert model_input["conversation_summary"]
            assert model_input["context_control"]["triggered"] is True
            assert estimate_tokens(long_answer) > 700
            client.delete(f"/api/projects/{project['id']}")
    finally:
        object.__setattr__(settings, "context_compression_trigger_tokens", original_trigger)
        object.__setattr__(settings, "context_compression_target_tokens", original_target)
        object.__setattr__(settings, "context_recent_turns", original_recent)


def test_vision_opening_labels_image_information_as_unconfirmed_candidates() -> None:
    opening = opening_from_observation({
        "status": "ready",
        "observations": {
            "people_count": 3,
            "scene": "公司前台",
            "visible_text": ["腾讯"],
            "objects": ["工牌", "花束"],
            "time_clues": [{"value": "傍晚", "confidence": "medium"}],
            "place_clues": [{"value": "法国巴黎", "confidence": "high"}],
        },
    })
    assert "有点像法国巴黎" in opening
    assert "可能看错" in opening
    assert opening.count("？") == 1
    assert "3个人" not in opening
    assert "公司前台" not in opening
    assert "腾讯" not in opening
    assert "工牌" not in opening
    assert "傍晚" not in opening
    assert "Lisa" not in opening

    frontend = (settings.root_dir / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "人物画面线索" not in frontend
    assert "时间猜测" not in frontend
    assert "地点猜测" not in frontend


def test_interview_boundary_and_one_question() -> None:
    with TestClient(app) as client:
        project = create_project(client, "访谈边界")
        _, session = upload_story(client, project["id"], ["这件事我记不清了，也不想继续说这个细节。"])
        assistant = session["turns"][-1]["content"]
        assert "爷爷" not in assistant and "奶奶" not in assistant
        assert assistant.count("？") <= 1
        assert len(assistant) >= 60
        assert "不需要勉强" in assistant or "不用像考试" in assistant
        assert session["turn_count"] == 1
        client.delete(f"/api/projects/{project['id']}")


def test_interview_output_guard_removes_extra_questions_and_inference() -> None:
    decision = {
        "reply": "您眼里都是亮亮的。您当时一定又期待又紧张。您还记得吗？",
        "question": "那是哪一年？又是在哪里拍的？",
        "ready_to_draft": False,
        "reason": "继续了解",
    }
    turns = [{"role": "user", "content": "这是我第一次加入公司的时候，我很开心。"}]
    normalized = normalize_interview_output(decision, turns, [])
    assert "眼里" not in normalized["reply"]
    assert "紧张" not in normalized["reply"]
    assert "？" not in normalized["reply"]
    assert normalized["reply"] == ""
    assert normalized["question"] == "那是哪一年？"


def test_interview_removes_tasklike_recording_language() -> None:
    decision = {
        "reply": (
            "您说这是去年在东方明珠拍的，我记住了，是去年的事呢。"
            "东方明珠一直是上海很标志的地方，您把那一刻留在那里，想必也有缘由。"
        ),
        "question": "当时是怎么想到去东方明珠那边的呢？",
        "ready_to_draft": False,
        "reason": "继续聊背后的故事",
    }
    turns = [{"role": "user", "content": "这是去年在东方明珠拍的。"}]
    normalized = normalize_interview_output(decision, turns, [])
    assert "我记住了" not in normalized["reply"]
    assert "想必" not in normalized["reply"]
    assert "标志的地方" not in normalized["reply"]
    assert "？" not in normalized["reply"]
    assert normalized["question"] == "当时是怎么想到去东方明珠那边的呢？"


def test_interview_uses_adaptive_length_without_opening_detector_or_padding_library() -> None:
    short = _interview_reply_length_guidance("是妈妈带我去的。")
    long = _interview_reply_length_guidance(
        "那天是妈妈带我去的，因为她希望我以后好好学习。后来我们在学校里走了很久，我第一次对大学有了具体的想象。"
    )
    assert short["suggested_min_chars"] < long["suggested_min_chars"]
    assert short["suggested_max_chars"] < long["suggested_max_chars"]
    agents_source = (settings.root_dir / "app" / "agents.py").read_text(encoding="utf-8")
    prompts_source = (settings.root_dir / "app" / "prompts.py").read_text(encoding="utf-8")
    assert "_has_repeated_reply_opening" not in agents_source
    assert "_ensure_interview_reply_length" not in agents_source
    assert "不要使用固定开场句库" in prompts_source


def test_interview_length_guidance_does_not_add_story_content() -> None:
    guidance = _interview_reply_length_guidance(
        "我们对着东方明珠拍了照，本来想坐船，但觉得太贵就没坐，而且人很多。"
    )
    assert set(guidance) == {"suggested_min_chars", "suggested_max_chars", "principle"}
    assert guidance["suggested_min_chars"] < guidance["suggested_max_chars"]
    assert "天气" not in guidance["principle"]
    assert "照片" not in guidance["principle"]


def test_interview_tracks_unexplained_motivation_then_ambiguous_route() -> None:
    turns = [
        {
            "role": "user",
            "content": "我小时候就很想去上海玩，然后刚好趁着国庆就去南京前往上海玩了。",
        }
    ]
    first = interview_priority_question(turns)
    assert first is not None
    assert "为什么" in first or "什么让" in first
    assert "上海" in first

    turns.extend(
        [
            {"role": "assistant", "content": first},
            {"role": "user", "content": "小时候经常在电视里看到上海，所以很向往。"},
        ]
    )
    second = interview_priority_question(turns)
    assert second is not None
    assert "南京" in second
    assert "什么关系" in second


def test_unresolved_lead_blocks_premature_drafting() -> None:
    decision = {
        "reply": "您和好朋友终于到了小时候向往的地方，这段经历确实值得慢慢说。",
        "question": "",
        "ready_to_draft": True,
        "reason": "已经回答五轮",
    }
    turns = [
        {"role": "user", "content": "我小时候就很想去上海玩，然后从南京前往上海。"},
    ]
    normalized = normalize_interview_output(decision, turns, [])
    assert normalized["ready_to_draft"] is False
    assert "上海" in normalized["question"]
    assert normalized["question"].count("？") == 1


def test_chapter_length_target_grows_with_interview_depth() -> None:
    short_turns = [{"role": "user", "content": "一段回答"}]
    rich_turns = [
        {"role": "user", "content": f"第{index}段回答里包含人物、缘起、行程、现场和自己的真实感受。"}
        for index in range(6)
    ]
    short_target = _chapter_target_length(short_turns)
    rich_target = _chapter_target_length(rich_turns)
    assert short_target == (450, 750)
    assert rich_target[0] > short_target[0]
    assert rich_target[0] >= 900
    assert rich_target[1] <= 1600


def test_deterministic_review_rejects_unsupported_scene_padding() -> None:
    content = (
        "我第一次去看东方明珠，那里人很多，也很挤。"
        "队伍移动得很慢，周围都是游客，我一直盯着灯光看。"
        "出发时是什么心情，我已经记不清了，只记得很期待。"
    )
    facts = [
        {"id": "f1", "fact_type": "event", "value": "第一次去见东方明珠"},
        {"id": "f2", "fact_type": "event", "value": "人很多很挤"},
    ]
    review = enforce_grounding_review(
        {"passed": True, "issues": [], "corrected_content": content},
        content,
        facts,
        [{"role": "user", "content": "第一次去见东方明珠的时候人好多，很挤。"}],
    )
    assert review["passed"] is False
    assert "人很多" in review["corrected_content"]
    assert "游客" not in review["corrected_content"]
    assert "记不清" not in review["corrected_content"]
    assert "灯光" not in review["corrected_content"]


def test_deterministic_review_accepts_visible_scene_evidence() -> None:
    content = "门口的灯光落在褪色的工作服上，我站在左边第五个位置。"
    review = enforce_grounding_review(
        {"passed": True, "issues": [], "corrected_content": content},
        content,
        [{"id": "f1", "fact_type": "event", "value": "我站在左边第五个位置"}],
        [],
        [{"id": "v1", "kind": "scene", "text": "车间门口有灯光，人物穿着工作服"}],
    )
    assert review["passed"] is True
    assert review["corrected_content"] == content


def test_deterministic_review_repairs_cross_chapter_action_drift() -> None:
    content = "我给安安讲我在烟盒纸上画的半毫米，也讲成都站台上我拎着帆布包。"
    facts = [
        {"id": "f1", "fact_type": "event", "value": "我把不合格零件的编号抄在烟盒纸上"},
        {"id": "f2", "fact_type": "event", "value": "我低头替女儿整理帆布包带"},
    ]
    review = enforce_grounding_review(
        {"passed": True, "issues": [], "corrected_content": content},
        content,
        facts,
    )
    assert review["passed"] is False
    assert "烟盒纸上记下的半毫米" in review["corrected_content"]
    assert "替小雨理帆布包带" in review["corrected_content"]
    assert "画的半毫米" not in review["corrected_content"]


def test_interview_output_guard_removes_invented_scene_and_avoids_repeated_fallback() -> None:
    decision = {
        "reply": "Lisa开朗的笑容，让我仿佛看见了前台热热闹闹的画面。",
        "question": "那天公司是什么样子，或者您先遇见了谁？",
        "ready_to_draft": False,
        "reason": "继续了解",
    }
    turns = [
        {"role": "user", "content": "那是2015年，我刚大学毕业。"},
        {"role": "assistant", "content": "上一轮回应"},
        {"role": "user", "content": "是在前台和我的两个同事Lisa和Momo。"},
        {"role": "assistant", "content": "上一轮回应"},
        {"role": "user", "content": "是Lisa，她很开朗，也是她第一个接待我的。"},
    ]
    normalized = normalize_interview_output(decision, turns, [])
    assert "笑容" not in normalized["reply"]
    assert "热热闹闹" not in normalized["reply"]
    assert "您刚才说到" not in normalized["reply"]
    assert normalized["reply"] == ""
    assert normalized["question"] == "那天公司是什么样子？"


def test_ready_reply_removes_overwritten_praise_and_returns_choice_to_user() -> None:
    decision = {
        "reply": (
            "原来那天的新奇里，还藏着从四川小村子一步步走到上海的欢喜呀。"
            "您能这样记住自己的来路，真的很有心。"
            "谢谢您愿意把这一天讲给我听，我听着也替您高兴。"
            "我们已经有一段完整的材料了，可以先生成一版章节，您之后还可以继续修改。"
        ),
        "question": "",
        "ready_to_draft": True,
        "reason": "可以先整理",
    }
    turns = [
        {"role": "user", "content": "我是从四川的村子来到上海工作的，那天我很开心。"},
    ]
    normalized = normalize_interview_output(decision, turns, [])
    assert "藏着" not in normalized["reply"]
    assert "一步步" not in normalized["reply"]
    assert "来路" not in normalized["reply"]
    assert "很有心" not in normalized["reply"]
    assert "完整的材料" not in normalized["reply"]
    assert "生成一版" not in normalized["reply"]
    assert normalized["reply"] == ""
    assert normalized["question"] == ""


def test_fact_correction_and_exclusion() -> None:
    with TestClient(app) as client:
        project = create_project(client, "事实纠错")
        _, session = upload_story(
            client,
            project["id"],
            ["这是1985年在县城拍的。照片里还有一位同事，但我不想把他的事情写进书里。"],
        )
        assert len(session["facts"]) == 2
        first, second = session["facts"]
        corrected = client.patch(
            f"/api/facts/{first['id']}", json={"value": "这是1986年在县城拍的"}
        )
        assert corrected.status_code == 200
        assert corrected.json()["supersedes"] == first["id"]
        excluded = client.patch(
            f"/api/facts/{second['id']}", json={"include_in_book": False, "sensitivity": "sensitive"}
        )
        assert excluded.status_code == 200
        chapter = generate(client, session["id"])
        content = chapter["current_version"]["content"]
        assert "1986年" in content
        assert "1985年" not in content
        assert "一位同事" not in content
        client.delete(f"/api/projects/{project['id']}")


def test_chapter_uses_retrieval_snapshot() -> None:
    with TestClient(app) as client:
        project = create_project(client, "方法卡检索")
        _, session = upload_story(
            client,
            project["id"],
            ["这是我1988年刚参加工作时和同事在车间拍的合影。"],
        )
        chapter = generate(client, session["id"])
        snapshot = json.loads(chapter["current_version"]["source_snapshot_json"])
        method_ids = snapshot["writing_method_ids"]
        assert 2 <= len(method_ids) <= 4
        assert all(method_id.startswith("WM-") for method_id in method_ids)
        assert len(snapshot["fact_ids"]) >= 1
        assert "visual_evidence" in snapshot
        assert "literary_inferences" in snapshot
        client.delete(f"/api/projects/{project['id']}")


def test_confirmed_version_share_and_revoke() -> None:
    with TestClient(app) as client:
        project = create_project(client, "分享版本")
        _, session = upload_story(client, project["id"], ["这是我第一次参加工作的留影。"])
        chapter = generate(client, session["id"])
        blocked = client.post(f"/api/chapters/{chapter['id']}/shares")
        assert blocked.status_code == 409
        confirmed = client.post(f"/api/chapters/{chapter['id']}/confirm").json()
        assert "下次" in confirmed["next_story_suggestion"]
        assert "不着急" in confirmed["next_story_suggestion"]
        shared_text = confirmed["current_version"]["content"]
        link = client.post(f"/api/chapters/{chapter['id']}/shares").json()
        public = client.get(f"/api/shares/by-token/{link['token']}")
        assert public.status_code == 200
        assert public.json()["content"] == shared_text
        public_page = client.get(f"/share/{link['token']}")
        assert public_page.status_code == 200
        assert "<img" not in public_page.text
        revised = client.post(
            f"/api/chapters/{chapter['id']}/revise", json={"instruction": "语言朴实一些"}
        )
        assert revised.status_code == 200
        still_old = client.get(f"/api/shares/by-token/{link['token']}").json()
        assert still_old["content"] == shared_text
        revoked = client.delete(f"/api/shares/{link['id']}")
        assert revoked.status_code == 200
        assert client.get(f"/api/shares/by-token/{link['token']}").status_code == 404
        client.delete(f"/api/projects/{project['id']}")


def test_revision_candidate_can_be_discarded_without_changing_chapter() -> None:
    with TestClient(app) as client:
        project = create_project(client, "候选稿放弃测试")
        _, session = upload_story(client, project["id"], ["1985年我第一次参加工作。"])
        chapter = generate(client, session["id"])
        original_version_id = chapter["current_version"]["id"]

        candidate = client.post(
            f"/api/chapters/{chapter['id']}/revise",
            json={"instruction": "语言更朴实一些", "mode": "style"},
        ).json()
        assert candidate["status"] == "pending"
        assert client.get(f"/api/chapters/{chapter['id']}").json()["current_version_id"] == original_version_id

        discarded = client.post(
            f"/api/chapter-revision-candidates/{candidate['id']}/discard"
        )
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "discarded"
        unchanged = client.get(f"/api/chapters/{chapter['id']}").json()
        assert unchanged["current_version_id"] == original_version_id
        assert unchanged["revision_candidate"] is None
        client.delete(f"/api/projects/{project['id']}")


def test_adopting_fact_correction_updates_memory_append_only() -> None:
    with TestClient(app) as client:
        project = create_project(client, "事实更正候选稿")
        _, session = upload_story(client, project["id"], ["1985年我第一次参加工作。"])
        fact = next(item for item in session["facts"] if "1985年" in item["value"])
        corrected_value = fact["value"].replace("1985年", "1986年")
        chapter = generate(client, session["id"])

        candidate_response = client.post(
            f"/api/chapters/{chapter['id']}/revise",
            json={
                "instruction": f"把‘{fact['value']}’改成‘{corrected_value}’",
                "mode": "fact",
            },
        )
        assert candidate_response.status_code == 200, candidate_response.text
        candidate = candidate_response.json()
        assert candidate["correction"]["proposals"][0]["fact_id"] == fact["id"]
        assert not candidate["correction"]["unmatched"]
        before_adoption = fetch_all("SELECT * FROM memory_facts WHERE id = ?", (fact["id"],))[0]
        assert before_adoption["status"] != "retracted"

        adopted_response = client.post(
            f"/api/chapter-revision-candidates/{candidate['id']}/adopt"
        )
        assert adopted_response.status_code == 200, adopted_response.text
        adopted = adopted_response.json()
        assert adopted["current_version"]["version_number"] == 2
        assert "1986年" in adopted["current_version"]["content"]
        facts = fetch_all(
            "SELECT * FROM memory_facts WHERE id = ? OR supersedes = ? ORDER BY created_at",
            (fact["id"], fact["id"]),
        )
        assert facts[0]["status"] == "retracted"
        assert facts[-1]["value"] == corrected_value
        assert facts[-1]["status"] == "confirmed_by_user"
        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        assert timeline[0]["start_year"] == 1986
        client.delete(f"/api/projects/{project['id']}")


def test_failed_review_cannot_be_confirmed() -> None:
    with TestClient(app) as client:
        project = create_project(client, "审校门禁")
        _, session = upload_story(client, project["id"], ["这是一次学校活动。"])
        chapter = generate(client, session["id"])
        execute(
            "UPDATE chapter_versions SET review_json = ? WHERE id = ?",
            (json.dumps({"passed": False, "issues": ["待核对"], "corrected_content": "草稿"}), chapter["current_version"]["id"]),
        )
        blocked = client.post(f"/api/chapters/{chapter['id']}/confirm")
        assert blocked.status_code == 409
        client.delete(f"/api/projects/{project['id']}")


def test_second_photo_merge_preserves_versions() -> None:
    with TestClient(app) as client:
        project = create_project(client, "章节合并")
        _, first_session = upload_story(
            client, project["id"], ["1985年我刚进车间，这是和师傅们的第一张合影。"], "first.png"
        )
        first_chapter = generate(client, first_session["id"])
        confirmed = client.post(f"/api/chapters/{first_chapter['id']}/confirm").json()
        original_version_id = confirmed["current_version"]["id"]
        old_link = client.post(f"/api/chapters/{first_chapter['id']}/shares").json()
        old_shared_content = client.get(f"/api/shares/by-token/{old_link['token']}").json()["content"]

        second_photo, second_session = upload_story(
            client, project["id"], ["这张也是同一年在车间拍的，记录了我第一次独立操作。"], "second.png"
        )
        second_chapter = generate(client, second_session["id"])
        merged = client.post(
            f"/api/photos/{second_photo['id']}/relation",
            json={
                "choice": "merge",
                "chapter_id": first_chapter["id"],
                "source_chapter_id": second_chapter["id"],
            },
        )
        assert merged.status_code == 200, merged.text
        target = merged.json()["chapter"]
        assert target["current_version"]["version_number"] == 2
        assert target["current_version"]["id"] != original_version_id
        assert len(target["versions"]) == 2
        assert target["status"] == "draft"
        assert len(target["photos"]) == 2
        assert client.get(f"/api/shares/by-token/{old_link['token']}").json()["content"] == old_shared_content
        visible = client.get(f"/api/projects/{project['id']}/chapters").json()
        assert [item["id"] for item in visible] == [first_chapter["id"]]
        client.delete(f"/api/projects/{project['id']}")


def test_book_weave_creates_grouped_immutable_versions() -> None:
    with TestClient(app) as client:
        project = create_project(client, "整书关联版")
        chapter_ids = []
        old_version_ids = []
        stories = [
            "1965年我抱着两本书在村小学门口拍照，那时喜欢临摹课本里的齿轮。",
            "1981年我在机械厂夜校学制图，陈国强把尺子推给我，让我从基准线重画。",
            "1985年我发现夹具差半毫米，陈国强陪我试零件，后来我被叫到前排合影。",
        ]
        for index, story in enumerate(stories, 1):
            _, session = upload_story(client, project["id"], [story], f"weave-{index}.png")
            chapter = generate(client, session["id"])
            confirmed = client.post(f"/api/chapters/{chapter['id']}/confirm").json()
            chapter_ids.append(chapter["id"])
            old_version_ids.append(confirmed["current_version"]["id"])

        woven_response = client.post(f"/api/projects/{project['id']}/weave")
        assert woven_response.status_code == 200
        edition = woven_response.json()
        assert edition["edition_number"] == 1
        assert len(edition["chapters"]) == 3
        assert edition["review"]["passed"] is True
        assert [item["chapter_id"] for item in edition["chapters"]] == chapter_ids
        assert all(item["version_id"] not in old_version_ids for item in edition["chapters"])
        for item in edition["chapters"]:
            detail = client.get(f"/api/chapters/{item['chapter_id']}").json()
            assert len(detail["versions"]) == 2
            source = json.loads(detail["current_version"]["source_snapshot_json"])
            assert source["book_edition_id"] == edition["id"]
            assert "director_brief" in source

        confirmed_edition = client.post(f"/api/book-editions/{edition['id']}/confirm")
        assert confirmed_edition.status_code == 200
        assert confirmed_edition.json()["status"] == "confirmed"
        assert all(item["confirmed_at"] for item in confirmed_edition.json()["chapters"])
        assert len(client.get(f"/api/projects/{project['id']}/book-editions").json()) == 1
        client.delete(f"/api/projects/{project['id']}")


def test_living_autobiography_starts_with_one_photo_and_grows_by_version() -> None:
    with TestClient(app) as client:
        project = create_project(client, "一张照片也能成书", person="first")
        first_photo, first_session = upload_story(
            client,
            project["id"],
            ["1985年我在机械厂门口合影，那次我发现夹具偏了半毫米。"],
            "one-photo-book.png",
        )
        first_chapter = generate(client, first_session["id"])
        original_version_id = first_chapter["current_version"]["id"]
        original_content = first_chapter["current_version"]["content"]

        first_response = client.post(f"/api/projects/{project['id']}/autobiography/compile")
        assert first_response.status_code == 200, first_response.text
        first_edition = first_response.json()
        assert first_edition["edition_number"] == 1
        assert first_edition["scope"] == "micro"
        assert first_edition["narrative_person"] == "third"
        assert first_edition["review"]["source_coverage"] == 1
        assert first_edition["review"]["photo_coverage"] == 1
        assert first_photo["id"] in first_edition["manuscript"]["sections"][0]["photo_ids"]
        unchanged = client.get(f"/api/chapters/{first_chapter['id']}").json()
        assert unchanged["current_version"]["id"] == original_version_id
        assert unchanged["current_version"]["content"] == original_content

        second_photo, second_session = upload_story(
            client,
            project["id"],
            ["2001年她送女儿去外地上大学，手里一直攥着录取通知书。"],
            "second-photo-book.png",
        )
        second_chapter = generate(client, second_session["id"])
        second_response = client.post(f"/api/projects/{project['id']}/autobiography/compile")
        assert second_response.status_code == 200, second_response.text
        second_edition = second_response.json()
        assert second_edition["edition_number"] == 2
        assert second_edition["previous_edition_id"] == first_edition["id"]
        assert second_edition["scope"] == "growing"
        assert second_edition["source_snapshot"]["added_chapter_ids"] == [second_chapter["id"]]
        used_photos = {
            photo_id
            for section in second_edition["manuscript"]["sections"]
            for photo_id in section["photo_ids"]
        }
        assert used_photos == {first_photo["id"], second_photo["id"]}
        editions = client.get(f"/api/projects/{project['id']}/autobiography-editions").json()
        assert [edition["edition_number"] for edition in editions] == [2, 1]
        client.delete(f"/api/projects/{project['id']}")


def test_project_export_and_delete() -> None:
    with TestClient(app) as client:
        project = create_project(client, "导出删除")
        photo, session = upload_story(client, project["id"], ["这是一次家庭聚会。"], "delete.png")
        chapter = generate(client, session["id"])
        client.post(f"/api/chapters/{chapter['id']}/confirm")
        link = client.post(f"/api/chapters/{chapter['id']}/shares").json()
        exported = client.get(f"/api/projects/{project['id']}/export")
        assert exported.status_code == 200
        body = exported.json()
        assert body["project"]["id"] == project["id"]
        assert body["sessions"][0]["turns"]
        assert body["chapters"][0]["versions"]
        assert body["shares"][0]["token"] == link["token"]
        assert body["model_runs"]
        media_path = settings.media_dir / photo["stored_name"]
        assert media_path.exists()
        deleted = client.delete(f"/api/projects/{project['id']}")
        assert deleted.status_code == 200
        assert not media_path.exists()
        assert client.get(f"/api/projects/{project['id']}").status_code == 404
        assert client.get(f"/api/shares/by-token/{link['token']}").status_code == 404
        assert fetch_all("SELECT * FROM model_runs WHERE project_id = ?", (project["id"],)) == []


def test_model_runs_are_audited() -> None:
    with TestClient(app) as client:
        before = len(fetch_all("SELECT * FROM model_runs"))
        project = create_project(client, "模型审计")
        _, session = upload_story(client, project["id"], ["这是在学校拍的毕业照。"])
        generate(client, session["id"])
        runs = fetch_all("SELECT * FROM model_runs ORDER BY created_at")
        assert len(runs) >= before + 4
        assert {run["agent_name"] for run in runs[-4:]} >= {
            "memory_agent", "interview_agent", "chapter_agent", "review_agent"
        }
        assert all(run["provider"] == "mock" for run in runs[-4:])
        client.delete(f"/api/projects/{project['id']}")


def test_people_catalog_formats_confirmed_people_and_uses_revision_cache() -> None:
    with TestClient(app) as client:
        project = create_project(client, "人物簿测试")
        photo, session = upload_story(
            client,
            project["id"],
            ["1985年照片里是我和两位大学同学，我们一起参加了学校的毕业活动。"],
            "people.png",
        )
        chapter = generate(client, session["id"])

        first = client.get(f"/api/projects/{project['id']}/people")
        assert first.status_code == 200, first.text
        catalog = first.json()
        assert catalog["counts"]["all"] >= 2
        assert any(person["kind"] == "protagonist" for person in catalog["people"])
        confirmed = next(person for person in catalog["people"] if person["kind"] == "confirmed")
        assert "同学" in confirmed["relationship"] or "同学" in confirmed["summary"]
        assert confirmed["key_attributes"]
        assert photo["id"] in confirmed["photo_ids"]
        assert chapter["title"] in confirmed["chapter_titles"]
        assert confirmed["source_fact_ids"]

        runs_after_first = len(fetch_all(
            "SELECT id FROM model_runs WHERE project_id = ? AND agent_name = 'people_curator'",
            (project["id"],),
        ))
        second = client.get(f"/api/projects/{project['id']}/people")
        assert second.status_code == 200
        assert second.json()["source_fingerprint"] == catalog["source_fingerprint"]
        runs_after_second = len(fetch_all(
            "SELECT id FROM model_runs WHERE project_id = ? AND agent_name = 'people_curator'",
            (project["id"],),
        ))
        assert runs_after_second == runs_after_first == 1

        frontend = (settings.root_dir / "app" / "static" / "index.html").read_text(encoding="utf-8")
        frontend_js = (settings.root_dir / "app" / "static" / "app.js").read_text(encoding="utf-8")
        assert 'id="peopleButton"' in frontend
        assert 'id="peopleDialog"' in frontend
        assert "person-photo-strip" not in frontend_js
        client.delete(f"/api/projects/{project['id']}")


def test_deleting_photo_preserves_interview_chapter_and_autobiography_by_default() -> None:
    with TestClient(app) as client:
        project = create_project(client, "照片与故事分离删除")
        photo, session = upload_story(
            client,
            project["id"],
            ["2015年大学毕业时，我和同学在校门口拍下了这张照片。"],
            "keep-story.png",
        )
        chapter = generate(client, session["id"])
        timeline_before_delete = client.get(f"/api/projects/{project['id']}/timeline").json()
        assert timeline_before_delete[0]["summary_source"] == "chapter"
        assert timeline_before_delete[0]["display_summary"]
        assert timeline_before_delete[0]["display_summary"] != timeline_before_delete[0]["summary"]
        autobiography = client.post(f"/api/projects/{project['id']}/autobiography/compile").json()
        original_chapter = chapter["current_version"]["content"]
        original_book = autobiography["manuscript"]["sections"][0]["content"]
        media_path = settings.media_dir / photo["stored_name"]
        assert media_path.exists()

        deleted = client.delete(f"/api/photos/{photo['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["mode"] == "asset_only"
        assert deleted.json()["content_preserved"] is True
        assert not media_path.exists()

        refreshed = client.get(f"/api/projects/{project['id']}").json()
        assert refreshed["photos"] == []
        assert refreshed["timeline"][0]["photos"] == []
        assert refreshed["timeline"][0]["photo_removed"] is True
        preserved_session = client.get(f"/api/sessions/{session['id']}").json()
        assert preserved_session["turns"]
        assert preserved_session["facts"]
        assert preserved_session["photo"]["is_deleted"] is True
        preserved_chapter = client.get(f"/api/chapters/{chapter['id']}").json()
        assert preserved_chapter["current_version"]["content"] == original_chapter
        assert preserved_chapter["photos"] == []
        preserved_book = client.get(f"/api/autobiography-editions/{autobiography['id']}").json()
        assert preserved_book["manuscript"]["sections"][0]["content"] == original_book
        assert preserved_book["manuscript"]["sections"][0]["photos"] == []
        assert client.get(f"/api/photos/{photo['id']}/observation").status_code == 410
        client.delete(f"/api/projects/{project['id']}")


def test_explicit_photo_and_story_deletion_removes_derived_content() -> None:
    with TestClient(app) as client:
        project = create_project(client, "明确删除照片故事")
        photo, session = upload_story(
            client,
            project["id"],
            ["2008年第一次参加工作，拍下这张照片纪念。"],
            "delete-story.png",
        )
        chapter = generate(client, session["id"])
        autobiography = client.post(f"/api/projects/{project['id']}/autobiography/compile").json()

        deleted = client.delete(f"/api/photos/{photo['id']}?delete_content=true")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["mode"] == "asset_and_story"
        assert deleted.json()["content_preserved"] is False
        assert deleted.json()["removed_chapters"] == 1
        assert client.get(f"/api/photos/{photo['id']}/observation").status_code == 404
        assert client.get(f"/api/sessions/{session['id']}").status_code == 404
        assert client.get(f"/api/chapters/{chapter['id']}").status_code == 404
        assert client.get(f"/api/autobiography-editions/{autobiography['id']}").status_code == 404
        refreshed = client.get(f"/api/projects/{project['id']}").json()
        assert refreshed["photos"] == []
        assert refreshed["timeline"] == []
        assert refreshed["chapters"] == []
        client.delete(f"/api/projects/{project['id']}")


def test_timeline_orders_by_life_time_and_backfills_old_event_without_overwriting_chapter() -> None:
    with TestClient(app) as client:
        project = create_project(client, "逆序人生时间线")

        _, first_session = upload_story(
            client,
            project["id"],
            ["2015年我大学毕业后加入腾讯，那是我第一次参加工作。"],
            "2015-work.png",
        )
        first_chapter = generate(client, first_session["id"])
        original_content = first_chapter["current_version"]["content"]
        client.post(f"/api/chapters/{first_chapter['id']}/confirm")

        _, second_session = upload_story(
            client,
            project["id"],
            ["2012年我还在大学读书，这是一次校园活动。"],
            "2012-campus.png",
        )
        cross_reply = client.post(
            f"/api/sessions/{second_session['id']}/reply",
            json={"text": "2015年加入腾讯的时候，是Lisa第一个接待我的。"},
        )
        assert cross_reply.status_code == 200

        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        assert [event["start_year"] for event in timeline] == [2012, 2015]
        event_2012, event_2015 = timeline
        assert all("Lisa" not in fact["value"] for fact in event_2012["facts"])
        assert any("Lisa" in fact["value"] for fact in event_2015["facts"])
        assert event_2015["needs_chapter_refresh"] == 1

        unchanged = client.get(f"/api/chapters/{first_chapter['id']}").json()
        assert unchanged["current_version"]["version_number"] == 1
        assert unchanged["current_version"]["content"] == original_content
        assert unchanged["has_new_memory"] is True

        second_chapter = generate(client, second_session["id"])
        assert "2012年" in second_chapter["current_version"]["content"]
        assert "Lisa" not in second_chapter["current_version"]["content"]
        ordered_chapters = client.get(f"/api/projects/{project['id']}/chapters").json()
        assert [chapter["timeline_year"] for chapter in ordered_chapters] == [2012, 2015]

        candidate = client.post(
            f"/api/chapters/{first_chapter['id']}/revise",
            json={"instruction": "补充新找到的事实"},
        ).json()
        still_unchanged = client.get(f"/api/chapters/{first_chapter['id']}").json()
        assert still_unchanged["current_version"]["version_number"] == 1
        revised = client.post(
            f"/api/chapter-revision-candidates/{candidate['id']}/adopt"
        ).json()
        assert revised["current_version"]["version_number"] == 2
        assert "Lisa" in revised["current_version"]["content"]
        assert revised["has_new_memory"] is False
        client.delete(f"/api/projects/{project['id']}")


def test_timeline_uses_literary_chapter_title_without_overwriting_archive_title() -> None:
    with TestClient(app) as client:
        project = create_project(client, "标题分层")
        photo, session = upload_story(
            client,
            project["id"],
            ["2010年我第一次离开家乡去外地工作，母亲送我到了车站。"],
            "title-layer.png",
        )
        chapter = generate(client, session["id"])
        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        event = timeline[0]
        assert event["primary_photo_id"] == photo["id"]
        assert event["archival_title"] == "测试照片"
        assert event["display_title"] == chapter["title"]
        assert event["title_source"] == "chapter"
        assert event["title"] == event["archival_title"]
        client.delete(f"/api/projects/{project['id']}")


def test_blank_photo_title_is_generated_from_vision_note_and_shared_memory() -> None:
    with TestClient(app) as client:
        project = create_project(client, "上传即生成文学标题")
        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("campus.png", PNG_BYTES, "image/png")},
            data={"note": "大学校园门口的一张旧照片", "title": ""},
        )
        assert uploaded.status_code == 200
        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        event = timeline[0]
        assert event["archival_title"] == "大学校园门口的一张旧照片"
        assert event["display_title"] == "那扇门后的远方"
        assert event["title_source"] == "local_fallback"
        assert event["title_versions"][-1]["stage"] == "upload"
        assert event["title_versions"][-1]["source_snapshot"]["shared_snapshot_id"]

        runs = fetch_all(
            "SELECT input_json FROM model_runs WHERE project_id = ? AND agent_name = 'title_agent'",
            (project["id"],),
        )
        assert len(runs) == 1
        title_input = json.loads(runs[0]["input_json"])
        assert "photo_observation" in title_input
        assert "shared_life_context" in title_input
        client.delete(f"/api/projects/{project['id']}")


def test_user_photo_title_has_priority_over_generated_and_chapter_titles() -> None:
    with TestClient(app) as client:
        project = create_project(client, "用户标题优先")
        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("station.png", PNG_BYTES, "image/png")},
            data={"note": "母亲在车站送我", "title": "那一次，她送我到站台尽头"},
        )
        assert uploaded.status_code == 200
        session = uploaded.json()["session"]
        replied = client.post(
            f"/api/sessions/{session['id']}/reply",
            json={"text": "2010年我第一次离开家乡工作，母亲把我送到了车站。"},
        )
        assert replied.status_code == 200
        generate(client, session["id"])

        event = client.get(f"/api/projects/{project['id']}/timeline").json()[0]
        assert event["display_title"] == "那一次，她送我到站台尽头"
        assert event["title_source"] == "user"
        assert any(version["source"] == "user" for version in event["title_versions"])
        assert fetch_all(
            "SELECT id FROM model_runs WHERE project_id = ? AND agent_name = 'title_agent'",
            (project["id"],),
        ) == []
        client.delete(f"/api/projects/{project['id']}")


def test_user_title_context_prevents_reasking_known_time_and_place() -> None:
    with TestClient(app) as client:
        project = create_project(client, "首问不重复已知信息")
        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("sjtu.png", PNG_BYTES, "image/png")},
            data={"title": "小学的时候第一次去参观上海交大", "note": ""},
        )
        assert uploaded.status_code == 200
        session = uploaded.json()["session"]
        assert len(session["turns"]) == 1
        opening = session["turns"][0]["content"]
        assert "小学" in opening
        assert "上海交大" in opening
        assert "在哪里" not in opening
        assert "什么时候" not in opening
        assert opening.count("？") == 1
        assert "为什么会想到" in opening

        # 零轮会话再次读取时不得被旧的纯识图模板覆盖或重复插入。
        refreshed_session = client.get(f"/api/sessions/{session['id']}").json()
        assert len(refreshed_session["turns"]) == 1
        assert refreshed_session["turns"][0]["content"] == opening

        event = client.get(f"/api/projects/{project['id']}/timeline").json()[0]
        assert event["time_text"] == "小学"
        assert event["time_precision"] == "life_stage"
        assert event["time_source_type"] == "user_title"
        assert event["location"] == "上海交大"
        client.delete(f"/api/projects/{project['id']}")


def test_each_timeline_story_can_reopen_its_saved_interview() -> None:
    with TestClient(app) as client:
        project = create_project(client, "逐照片查看访谈")
        _, first = upload_story(client, project["id"], ["这是第一张照片背后的故事。"], "first.png")
        _, second = upload_story(client, project["id"], ["这是第二张照片背后的故事。"], "second.png")

        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        session_ids = {event["primary_session_id"] for event in timeline}
        assert session_ids == {first["id"], second["id"]}
        for session_id in session_ids:
            reopened = client.get(f"/api/sessions/{session_id}")
            assert reopened.status_code == 200
            assert any(turn["role"] == "user" for turn in reopened.json()["turns"])

        frontend = (settings.root_dir / "app" / "static" / "app.js").read_text(encoding="utf-8")
        assert "查看访谈记录" in frontend
        assert "继续聊这张照片" in frontend
        assert "/api/sessions/${event.primary_session_id}" in frontend
        client.delete(f"/api/projects/{project['id']}")


def test_corrected_event_name_becomes_user_display_title() -> None:
    with TestClient(app) as client:
        project = create_project(client, "标题更正优先")
        photo, _ = upload_story(client, project["id"], [], "school-gate.png")
        event = client.get(f"/api/projects/{project['id']}/timeline").json()[0]
        corrected = client.patch(
            f"/api/timeline-events/{event['id']}",
            json={"title": "母亲指给我的那扇门"},
        )
        assert corrected.status_code == 200
        refreshed = client.get(f"/api/projects/{project['id']}/timeline").json()[0]
        assert refreshed["primary_photo_id"] == photo["id"]
        assert refreshed["display_title"] == "母亲指给我的那扇门"
        assert refreshed["title_source"] == "user"
        assert refreshed["title_versions"][-1]["stage"] == "correction"
        client.delete(f"/api/projects/{project['id']}")


def test_timeline_places_junior_high_before_last_year_graduation_trip() -> None:
    """复现：后上传的“初三”照片，应排在先上传的“去年大学毕业”照片之前。"""
    with TestClient(app) as client:
        project = create_project(client, "人生阶段混合排序")
        _, graduation_session = upload_story(
            client,
            project["id"],
            ["这是去年和大学同学的毕业旅行，那时我们正在聊毕业后找工作的事情。"],
            "graduation-trip.png",
        )
        _, junior_session = upload_story(
            client,
            project["id"],
            ["这是在交通大学门口拍的，是我初三的时候。"],
            "junior-high.png",
        )

        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        assert [event["primary_session_id"] for event in timeline] == [
            junior_session["id"],
            graduation_session["id"],
        ]
        assert timeline[0]["time_text"] == "初三"
        assert timeline[0]["sort_stage"] == "初三"
        assert timeline[0]["sort_basis"] == "life_stage_anchor"
        assert timeline[0]["sort_year_estimate"] < timeline[1]["start_year"]
        assert timeline[1]["time_precision"] == "approximate"
        assert "去年" in timeline[1]["time_text"]
        assert timeline[1]["sort_stage"] == "大学毕业"
        assert timeline[1]["sort_basis"] == "relative_date"
        client.delete(f"/api/projects/{project['id']}")


def test_photo_note_life_stage_is_locked_before_later_revisit() -> None:
    """照片说明属于用户证据；后来重访只能成为关联事件，不能覆盖照片时间。"""
    with TestClient(app) as client:
        project = create_project(client, "照片时间与重访事件")
        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("school-visit.png", PNG_BYTES + b"school-visit", "image/png")},
            data={"note": "这是我小学时候第一次参观上海交通大学的照片"},
        )
        assert uploaded.status_code == 200
        session = uploaded.json()["session"]
        initial = session["timeline_event"]
        assert initial["time_text"] == "小学"
        assert initial["time_precision"] == "life_stage"
        assert initial["time_locked"] == 1
        assert initial["time_source_type"] == "photo_note"

        response = client.post(
            f"/api/sessions/{session['id']}/reply",
            json={"text": "去年我又回去看了一次，站在校门口心里很唏嘘。"},
        )
        assert response.status_code == 200
        timeline = client.get(f"/api/projects/{project['id']}/timeline").json()
        event = timeline[0]
        assert event["time_text"] == "小学"
        assert event["start_year"] is None
        assert any(item["temporal_role"] == "later_related_event" for item in event["related_events"])
        assert any(item["start_year"] == 2025 for item in event["related_events"])
        client.delete(f"/api/projects/{project['id']}")


def test_one_reply_can_contain_capture_time_and_later_event() -> None:
    """同一句中的照片时间和后续时间必须拆成两个事件提及。"""
    with TestClient(app) as client:
        project = create_project(client, "一句多事件")
        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("two-times.png", PNG_BYTES + b"two-times", "image/png")},
            data={"note": "上海交通大学校门照片"},
        ).json()
        session_id = uploaded["session"]["id"]
        response = client.post(
            f"/api/sessions/{session_id}/reply",
            json={"text": "这张照片是我小学时拍的，去年我又回去看了一次。"},
        )
        assert response.status_code == 200
        event = client.get(f"/api/projects/{project['id']}/timeline").json()[0]
        roles = {item["temporal_role"] for item in event["event_mentions"]}
        assert event["time_text"] == "小学"
        assert "photo_capture_event" in roles
        assert "later_related_event" in roles
        client.delete(f"/api/projects/{project['id']}")


def test_explicit_time_correction_replaces_locked_capture_time() -> None:
    """只有用户明确更正时，才允许替换已经锁定的照片时间。"""
    with TestClient(app) as client:
        project = create_project(client, "时间更正")
        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("correction.png", PNG_BYTES + b"correction", "image/png")},
            data={"note": "这是小学时候拍的照片"},
        ).json()
        session_id = uploaded["session"]["id"]
        response = client.post(
            f"/api/sessions/{session_id}/reply",
            json={"text": "我记错了，不是小学，应该是初三的时候拍的。"},
        )
        assert response.status_code == 200
        event = client.get(f"/api/projects/{project['id']}/timeline").json()[0]
        assert event["time_text"] == "初三"
        assert event["time_source_type"] == "user_reply"
        assert any(item["temporal_role"] == "time_correction" for item in event["event_mentions"])
        client.delete(f"/api/projects/{project['id']}")
