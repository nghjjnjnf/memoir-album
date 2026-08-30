from __future__ import annotations

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
)


def test_complete_photo_to_confirmed_chapter_flow() -> None:
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["llm_mode"] == "mock"

        project = client.post(
            "/api/projects",
            json={"title": "测试自传", "narrative_person": "first"},
        ).json()

        uploaded = client.post(
            f"/api/projects/{project['id']}/photos",
            files={"image": ("memory.png", PNG_BYTES, "image/png")},
            data={"note": "一张老照片"},
        )
        assert uploaded.status_code == 200
        session = uploaded.json()["session"]

        answers = [
            "这是1985年在县城照相馆拍的，那时我刚参加工作。",
            "照片里是我和两个同事，我们在同一个车间上班。",
            "我那天很高兴，也有一点紧张，如今想起那段日子很踏实。",
        ]
        for answer in answers:
            reply = client.post(f"/api/sessions/{session['id']}/reply", json={"text": answer})
            assert reply.status_code == 200
            session = reply.json()

        assert session["status"] == "ready_to_draft"
        assert len(session["facts"]) >= 3

        generated = client.post(f"/api/sessions/{session['id']}/generate")
        assert generated.status_code == 200
        chapter = generated.json()
        assert chapter["current_version"]["version_number"] == 1
        assert "1985年" in chapter["current_version"]["content"]

        revised = client.post(
            f"/api/chapters/{chapter['id']}/revise",
            json={"instruction": "语言再朴实一些"},
        )
        assert revised.status_code == 200
        candidate = revised.json()
        assert candidate["status"] == "pending"
        unchanged = client.get(f"/api/chapters/{chapter['id']}").json()
        assert unchanged["current_version"]["version_number"] == 1
        assert len(unchanged["versions"]) == 1
        adopted = client.post(f"/api/chapter-revision-candidates/{candidate['id']}/adopt")
        assert adopted.status_code == 200
        assert adopted.json()["current_version"]["version_number"] == 2
        assert len(adopted.json()["versions"]) == 2

        confirmed = client.post(f"/api/chapters/{chapter['id']}/confirm")
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"
        assert confirmed.json()["current_version"]["confirmed_at"] is not None
