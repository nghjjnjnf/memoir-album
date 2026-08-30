from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable

import httpx

from .config import settings
from .context_memory import estimate_tokens, get_life_context_snapshot
from .db import execute, now_iso


Fallback = Callable[[], dict[str, Any]]


class LLMError(RuntimeError):
    pass


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return value


class LLMGateway:
    async def generate_json(
        self,
        agent_name: str,
        system_prompt: str,
        payload: dict[str, Any],
        fallback: Fallback,
    ) -> dict[str, Any]:
        project_id = self._find_project_id(payload)
        if project_id:
            payload = dict(payload)
            payload.setdefault("shared_life_context", get_life_context_snapshot(project_id))
            payload["_context_metadata"] = {
                "estimated_input_tokens": estimate_tokens(payload),
                "shared_snapshot_id": payload.get("shared_life_context", {}).get("snapshot_id"),
            }
            system_prompt = (
                system_prompt
                + "\n\n输入中的 shared_life_context 是所有 Agent 共用的人生主记忆。"
                "人物、关系和重要事件应与它保持一致；其中 key_facts 是事实依据，"
                "narrative_memory 只用于叙事连续性，绝不能反向建立新事实。"
            )
        run_id = str(uuid.uuid4())
        provider = "mock" if settings.use_mock_llm else "deepseek"
        output: dict[str, Any] = {}
        error: str | None = None
        status = "ok"
        try:
            if settings.use_mock_llm:
                output = fallback()
            else:
                if not settings.deepseek_api_key:
                    raise LLMError("未配置 DEEPSEEK_API_KEY")
                output = await self._deepseek(system_prompt, payload, agent_name)
        except Exception as exc:
            status = "fallback"
            error = str(exc)[:1000]
            output = fallback()
        finally:
            project_id = self._find_project_id(payload)
            execute(
                """
                INSERT INTO model_runs
                (id, project_id, agent_name, provider, model, input_json, output_json, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    agent_name,
                    provider,
                    settings.deepseek_model,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(output, ensure_ascii=False),
                    status,
                    error,
                    now_iso(),
                ),
            )
        return output

    def _find_project_id(self, value: Any) -> str | None:
        if isinstance(value, dict):
            direct = value.get("project_id")
            if isinstance(direct, str) and direct:
                return direct
            for nested in value.values():
                found = self._find_project_id(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = self._find_project_id(nested)
                if found:
                    return found
        return None

    async def _deepseek(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        agent_name: str,
    ) -> dict[str, Any]:
        if agent_name == "chapter_agent":
            max_tokens = 7000
        elif agent_name == "chapter_reweaver":
            max_tokens = 4500
        elif agent_name == "book_director":
            max_tokens = 3500
        elif agent_name == "chapter_fact_linker":
            max_tokens = 1800
        elif agent_name in {"autobiography_compiler", "autobiography_regroup_editor"}:
            max_tokens = 14000
        elif agent_name == "autobiography_reviewer":
            max_tokens = 3500
        else:
            max_tokens = 3000
        request_body = {
            "model": settings.deepseek_model,
            # V4 defaults to thinking mode. Structured JSON agents can spend their
            # entire completion budget in reasoning_content and return empty content.
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "请严格返回 json 对象。输入数据：\n" + json.dumps(payload, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.6
            if agent_name in {"interview_agent", "interview_reply_editor"}
            else 0.2
            if agent_name in {
                "chapter_agent",
                "chapter_reweaver",
                "review_agent",
                "book_director",
                "book_continuity_reviewer",
                "autobiography_compiler",
                "autobiography_regroup_editor",
                "autobiography_reviewer",
            } else 0.4,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}
        last_error: Exception | None = None
        attempts = 3 if agent_name in {"book_director", "chapter_reweaver", "book_continuity_reviewer"} else 2
        for _ in range(attempts):
            try:
                started = time.perf_counter()
                timeout = 240 if agent_name in {"autobiography_compiler", "autobiography_regroup_editor"} else 60
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{settings.deepseek_base_url}/chat/completions",
                        headers=headers,
                        json=request_body,
                    )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if not content:
                    raise LLMError("模型返回了空内容")
                result = _parse_json(content)
                result["_latency_ms"] = int((time.perf_counter() - started) * 1000)
                return result
            except Exception as exc:
                last_error = exc
        raise LLMError(f"DeepSeek 调用失败：{last_error}")


gateway = LLMGateway()
