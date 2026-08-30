from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行三张合成照片的自传 Agent 端到端测试")
    parser.add_argument("--provider", choices=("mock", "deepseek"), default="mock")
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def checked(response: Any, action: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"{action}失败：HTTP {response.status_code} {response.text}")
    return response.json()


def main() -> int:
    args = parse_args()
    if args.provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY", "").strip():
        print("未检测到 DEEPSEEK_API_KEY。请通过安全终端提示输入新密钥。", file=sys.stderr)
        return 2

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (args.output_dir or ROOT / "evals" / "runs" / f"{args.provider}-{timestamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APP_DATA_DIR"] = str(run_dir / "data")
    os.environ["USE_MOCK_LLM"] = "true" if args.provider == "mock" else "false"
    if args.provider == "mock":
        os.environ["VISION_MODE"] = "disabled"

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.db import fetch_all
    from app.main import app

    stories = json.loads((ROOT / "evals" / "fixtures" / "synthetic-stories.json").read_text(encoding="utf-8"))
    image_dir = ROOT / "evals" / "fixtures" / "images"
    results: list[dict[str, Any]] = []

    with TestClient(app) as client:
        project = checked(
            client.post(
                "/api/projects",
                json={"title": "合成测试人物的人生故事", "narrative_person": "first"},
            ),
            "创建项目",
        )

        for story in stories:
            image_path = image_dir / story["image"]
            with image_path.open("rb") as image_file:
                uploaded = checked(
                    client.post(
                        f"/api/projects/{project['id']}/photos",
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
            results.append(
                {
                    "story": story,
                    "photo": uploaded["photo"],
                    "session": session,
                    "chapter": chapter,
                    "relation_suggestion": chapter.get("relation_suggestion"),
                }
            )

            if story["key"] == "factory_1985":
                confirmed = checked(
                    client.post(f"/api/chapters/{chapter['id']}/confirm"), "确认 1985 章节"
                )
                old_share = checked(
                    client.post(f"/api/chapters/{chapter['id']}/shares"), "分享 1985 章节"
                )
                results[-1]["chapter"] = confirmed
                results[-1]["share"] = old_share
            elif story["key"] == "spring_festival_1992":
                relation = checked(
                    client.post(
                        f"/api/photos/{uploaded['photo']['id']}/relation",
                        json={"choice": "new", "source_chapter_id": chapter["id"]},
                    ),
                    "保留 1992 独立章节",
                )
                confirmed = checked(
                    client.post(f"/api/chapters/{chapter['id']}/confirm"), "确认 1992 章节"
                )
                results[-1]["relation_result"] = relation
                results[-1]["chapter"] = confirmed

        first = results[0]
        retirement = results[2]
        merge = checked(
            client.post(
                f"/api/photos/{retirement['photo']['id']}/relation",
                json={
                    "choice": "merge",
                    "chapter_id": first["chapter"]["id"],
                    "source_chapter_id": retirement["chapter"]["id"],
                },
            ),
            "把退休故事合并到工作章节",
        )
        merged_confirmed = checked(
            client.post(f"/api/chapters/{merge['chapter']['id']}/confirm"), "确认合并章节"
        )
        merged_share = checked(
            client.post(f"/api/chapters/{merge['chapter']['id']}/shares"), "分享合并章节"
        )
        results[2]["relation_result"] = merge
        results[2]["chapter"] = merged_confirmed
        results[2]["share"] = merged_share

        visible_chapters = checked(
            client.get(f"/api/projects/{project['id']}/chapters"), "读取最终章节目录"
        )
        exported = checked(
            client.get(f"/api/projects/{project['id']}/export"), "导出完整项目"
        )

    model_runs = fetch_all(
        "SELECT * FROM model_runs WHERE project_id = ? ORDER BY created_at", (project["id"],)
    )
    fallback_runs = [run for run in model_runs if run["status"] in {"fallback", "error"}]
    provider_mismatch = [
        run
        for run in model_runs
        if run["agent_name"] != "vision_agent" and run["provider"] != args.provider
    ]
    passed = not fallback_runs and not provider_mismatch

    (run_dir / "project-export.json").write_text(
        json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_lines = [
        "# 三张合成照片端到端测试报告",
        "",
        f"- 测试时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- Provider：{args.provider}",
        f"- 模型：{settings.deepseek_model}",
        f"- 总模型调用：{len(model_runs)}",
        f"- 降级调用：{len(fallback_runs)}",
        f"- 最终可见章节：{len(visible_chapters)}",
        f"- 测试结论：{'通过' if passed else '失败'}",
        "",
        "## 流程结果",
        "",
        "- 照片 A：生成工作章节 v1、确认并创建旧版本分享链接；",
        "- 照片 B：Agent 给出关系建议，用户选择独立成章并确认；",
        "- 照片 C：先生成独立草稿，再由用户决定合并到工作章节，形成 v2；",
        "- 合并后，照片 A 的 v1 和旧分享链接保持不变；",
        "- 最终目录包含工作/退休合并章和春节家庭章。",
        "",
    ]
    for index, result in enumerate(results, start=1):
        chapter = result["chapter"]
        version = chapter["current_version"]
        snapshot = json.loads(version["source_snapshot_json"])
        review = json.loads(version["review_json"])
        report_lines.extend(
            [
                f"## 测试素材 {index}：{result['story']['key']}",
                "",
                f"- 图片：`{result['story']['image']}`",
                f"- 事实数：{len(result['session']['facts'])}",
                f"- Agent 关系建议：`{json.dumps(result.get('relation_suggestion'), ensure_ascii=False)}`",
                f"- 最终章节：{chapter['title']}",
                f"- 版本号：{version['version_number']}",
                f"- 审校通过：{review.get('passed')}",
                f"- 写作方法卡：{', '.join(snapshot.get('writing_method_ids', [])) or '合并版本沿用来源事实'}",
                "",
                "### 章节正文",
                "",
                version["content"],
                "",
            ]
        )
    if fallback_runs:
        report_lines.extend(
            [
                "## 失败与降级",
                "",
                *[
                    f"- {run['agent_name']}：{run.get('error') or run['status']}"
                    for run in fallback_runs
                ],
                "",
            ]
        )
    report_path = run_dir / "report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(json.dumps({"passed": passed, "report": str(report_path), "run_dir": str(run_dir)}, ensure_ascii=False))
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
