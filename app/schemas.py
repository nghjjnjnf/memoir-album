from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    narrative_person: Literal["first", "third"] = "first"


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    narrative_person: Literal["first", "third"] | None = None


class ReplyCreate(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class RevisionCreate(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    mode: Literal["auto", "style", "fact"] = "auto"
    base_candidate_id: str | None = None


class RelationChoice(BaseModel):
    choice: Literal["new", "attach", "merge"]
    chapter_id: str | None = None
    source_chapter_id: str | None = None


class FactUpdate(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=2000)
    include_in_book: bool | None = None
    sensitivity: Literal["normal", "sensitive"] | None = None
    event_id: str | None = None


class TimelineEventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    time_text: str | None = Field(default=None, max_length=100)
    start_year: int | None = Field(default=None, ge=1800, le=2200)
    end_year: int | None = Field(default=None, ge=1800, le=2200)
    time_precision: Literal["exact", "year", "range", "decade", "approximate", "life_stage", "unknown"] | None = None
    location: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=1000)


class MemoryFactOutput(BaseModel):
    fact_type: Literal["person", "time", "place", "event", "feeling", "reflection", "quote", "other"] = "other"
    value: str = Field(min_length=1, max_length=2000)
    sensitivity: Literal["normal", "sensitive"] = "normal"


class MemoryEventProposal(BaseModel):
    title: str = Field(default="", max_length=100)
    time_text: str = Field(default="", max_length=100)
    start_year: int | None = Field(default=None, ge=1800, le=2200)
    end_year: int | None = Field(default=None, ge=1800, le=2200)
    time_precision: Literal["exact", "year", "range", "decade", "approximate", "life_stage", "unknown"] = "unknown"
    location: str = Field(default="", max_length=200)


class MemoryAgentOutput(BaseModel):
    facts: list[MemoryFactOutput] = Field(default_factory=list)
    current_event: MemoryEventProposal | None = None


class PhotoTitleOutput(BaseModel):
    title: str = Field(min_length=2, max_length=30)
    rationale: str = Field(default="", max_length=300)
    used_memory_fact_ids: list[str] = Field(default_factory=list)


class ConversationCompactionOutput(BaseModel):
    conversation_summary: str = Field(default="", max_length=2000)
    covered_topics: list[str] = Field(default_factory=list)
    unresolved_clues: list[str] = Field(default_factory=list)
    user_preferences: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    important_quotes: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class InterviewAgentOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=500)
    question: str = Field(default="", max_length=300)
    ready_to_draft: bool
    reason: str = Field(default="", max_length=300)


class EntityDeclaration(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: Literal["person", "place", "landmark", "time", "object", "event", "other"] = "other"
    source: str = Field(default="", max_length=120)
    conflicts: list[str] = Field(default_factory=list)


class ChapterAgentOutput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    used_fact_ids: list[str] = Field(default_factory=list)
    used_visual_ids: list[str] = Field(default_factory=list)
    literary_inferences: list[str] = Field(default_factory=list)
    entities: list[EntityDeclaration] = Field(default_factory=list)


class ChapterFactLinkOutput(BaseModel):
    fact_ids: list[str] = Field(default_factory=list)


class ReviewAgentOutput(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    corrected_content: str = Field(min_length=1)


class RelationAgentOutput(BaseModel):
    choice: Literal["new", "attach", "merge"]
    chapter_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)


class MergeAgentOutput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)


class BookDirectorOutput(BaseModel):
    book_arc: str = Field(min_length=1, max_length=1000)
    people_registry: list[dict[str, Any]] = Field(default_factory=list)
    narrative_threads: list[dict[str, Any]] = Field(default_factory=list)
    chapter_briefs: list[dict[str, Any]] = Field(default_factory=list)


class BookContinuityReviewOutput(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    thread_results: list[dict[str, Any]] = Field(default_factory=list)
    character_results: list[dict[str, Any]] = Field(default_factory=list)


class AutobiographySectionOutput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    source_chapter_ids: list[str] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
    character_revelation: str = Field(min_length=1, max_length=300)
    photo_meaning: str = Field(min_length=1, max_length=300)
    narrative_function: str = Field(min_length=1, max_length=200)


class AutobiographyManuscriptOutput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    subtitle: str = Field(default="", max_length=160)
    core_theme: str = Field(min_length=1, max_length=500)
    character_portrait: str = Field(min_length=1, max_length=800)
    preface: str = Field(min_length=1)
    sections: list[AutobiographySectionOutput] = Field(min_length=1)
    afterword: str = Field(min_length=1)


class AutobiographyReviewOutput(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    third_person_score: float = Field(ge=0, le=1)
    source_coverage: float = Field(ge=0, le=1)
    photo_coverage: float = Field(ge=0, le=1)
    value_expression_score: float = Field(ge=0, le=5)
    literary_quality_score: float = Field(ge=0, le=5)
    character_traits: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)


class PersonCatalogEntryOutput(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["protagonist", "confirmed", "visual_unknown"]
    relationship: str = Field(default="", max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    story_role: str = Field(default="", max_length=300)
    event_ids: list[str] = Field(default_factory=list)
    chapter_ids: list[str] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
    source_fact_ids: list[str] = Field(default_factory=list)


class PeopleCatalogOutput(BaseModel):
    overview: str = Field(min_length=1, max_length=500)
    people: list[PersonCatalogEntryOutput] = Field(default_factory=list)
