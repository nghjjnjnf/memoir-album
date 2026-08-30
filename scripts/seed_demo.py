from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# The public seed must be deterministic, free, and safe to run offline.
os.environ.setdefault("USE_MOCK_LLM", "true")
os.environ.setdefault("VISION_MODE", "disabled")


def checked(response: Any, action: str) -> dict[str, Any] | list[dict[str, Any]]:
    if response.status_code >= 400:
        raise RuntimeError(f"{action}失败：HTTP {response.status_code} {response.text}")
    return response.json()


def main() -> int:
    from fastapi.testclient import TestClient

    from app.main import app

    title = "合成示例：林秋云的人生相册"
    stories = json.loads(
        (ROOT / "evals" / "fixtures" / "synthetic-stories.json").read_text(encoding="utf-8")
    )
    image_dir = ROOT / "evals" / "fixtures" / "images"

    with TestClient(app) as client:
        projects = checked(client.get("/api/projects"), "读取项目")
        existing = next((item for item in projects if item.get("title") == title), None)
        if existing:
            project_id = existing["id"]
            print(
                json.dumps(
                    {
                        "created": False,
                        "project_id": project_id,
                        "url": f"http://127.0.0.1:8000/?project={project_id}&demo=1",
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        project = checked(
            client.post(
                "/api/projects",
                json={"title": title, "narrative_person": "third"},
            ),
            "创建演示项目",
        )
        project_id = project["id"]

        for story in stories:
            image_path = image_dir / story["image"]
            with image_path.open("rb") as image_file:
                uploaded = checked(
                    client.post(
                        f"/api/projects/{project_id}/photos",
                        files={"image": (image_path.name, image_file, "image/png")},
                        data={"note": story["key"]},
                    ),
                    f"上传 {story['image']}",
                )

            session = uploaded["session"]
            for answer in story["answers"]:
                session = checked(
                    client.post(f"/api/sessions/{session['id']}/reply", json={"text": answer}),
                    f"访谈 {story['key']}",
                )
            chapter = checked(
                client.post(f"/api/sessions/{session['id']}/generate"),
                f"生成章节 {story['key']}",
            )
            checked(client.post(f"/api/chapters/{chapter['id']}/confirm"), "确认章节")

        edition = checked(
            client.post(f"/api/projects/{project_id}/autobiography/compile"),
            "编排完整自传",
        )

    print(
        json.dumps(
            {
                "created": True,
                "project_id": project_id,
                "chapters": len(stories),
                "book_title": edition.get("title"),
                "url": f"http://127.0.0.1:8000/?project={project_id}&demo=1",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
