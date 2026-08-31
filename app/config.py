from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT_DIR
    data_dir: Path = Path(os.getenv("APP_DATA_DIR", str(ROOT_DIR / "data"))).resolve()
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    use_mock_llm: bool = _as_bool(os.getenv("USE_MOCK_LLM"), True)
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "10"))
    vision_mode: str = os.getenv("VISION_MODE", "disabled").strip().lower()
    deepseek_vision_api_key: str = os.getenv("DEEPSEEK_VISION_API_KEY", "").strip()
    deepseek_vision_base_url: str = os.getenv("DEEPSEEK_VISION_BASE_URL", "https://api.deepseek.com").rstrip("/")
    vision_model: str = os.getenv("VISION_MODEL", "deepseek-v4-flash-vision-exp").strip()
    vision_timeout_seconds: int = int(os.getenv("VISION_TIMEOUT_SECONDS", "240"))
    context_compression_trigger_tokens: int = int(os.getenv("CONTEXT_COMPRESSION_TRIGGER_TOKENS", "6000"))
    context_compression_target_tokens: int = int(os.getenv("CONTEXT_COMPRESSION_TARGET_TOKENS", "3500"))
    context_recent_turns: int = int(os.getenv("CONTEXT_RECENT_TURNS", "10"))
    life_snapshot_max_people: int = int(os.getenv("LIFE_SNAPSHOT_MAX_PEOPLE", "24"))
    life_snapshot_max_events: int = int(os.getenv("LIFE_SNAPSHOT_MAX_EVENTS", "30"))
    min_chapter_chars: int = int(os.getenv("MIN_CHAPTER_CHARS", "500"))
    literary_quality_review_enabled: bool = _as_bool(os.getenv("LITERARY_QUALITY_REVIEW"), True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "life_story.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def vision_enabled(self) -> bool:
        return self.vision_mode == "deepseek_api"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.media_dir.mkdir(parents=True, exist_ok=True)
