from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .config import settings


METHODS_PATH = settings.root_dir / "knowledge" / "writing-methods" / "methods.json"


@lru_cache(maxsize=1)
def load_method_cards() -> list[dict[str, Any]]:
    with METHODS_PATH.open("r", encoding="utf-8") as file:
        cards = json.load(file)
    if not isinstance(cards, list):
        raise ValueError("写作方法库必须是 JSON 数组")
    return cards


def retrieve_method_cards(
    narrative_person: str,
    text: str,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """轻量本地检索；只返回写作方法，不保存或迁移他人人生事实。"""
    normalized = text.lower()
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for card in load_method_cards():
        score = 0
        if narrative_person in card.get("narrative_person", []):
            score += 2
        for keyword in card.get("keywords", []):
            if str(keyword).lower() in normalized:
                score += 3
        if card.get("id") in {"WM-001", "WM-002", "WM-008"}:
            score += 1
        scored.append((score, str(card.get("id", "")), card))
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [item[2] for item in scored[: max(2, min(limit, 4))]]
    return [
        {
            "id": card["id"],
            "title": card["title"],
            "method": card["method"],
            "avoid": card["avoid"],
            "example_input": card.get("example_input", ""),
            "positive_example": card.get("positive_example", ""),
            "learning_point": card.get("learning_point", ""),
        }
        for card in selected
    ]
