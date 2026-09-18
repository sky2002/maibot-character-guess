from typing import Dict, List, Literal
from urllib.parse import urlsplit

import json
import re
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator


ANSWERS = {
    "是": "是",
    "否": "否",
    "不是": "否",
    "不知道": "不知道",
    "不确定": "不知道",
    "可能是": "可能是",
    "大概是": "可能是",
    "可能不是": "可能不是",
    "大概不是": "可能不是",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    title: str = Field(max_length=300)
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(c.isspace() for c in value):
            raise ValueError("来源必须是完整 HTTP/HTTPS 地址")
        return value


class Candidate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    work: str = Field(max_length=150)
    facts: List[str] = Field(max_length=12)
    conflicts: List[str] = Field(max_length=12)
    source_urls: List[str] = Field(max_length=12)


class Research(StrictModel):
    summary: str = Field(max_length=8000)
    candidates: List[Candidate] = Field(max_length=20)
    sources: List[Source] = Field(max_length=25)
    uncertainties: List[str] = Field(max_length=20)


class Evidence(StrictModel):
    query: str
    backend: str
    research: Research
    created_at: float = Field(default_factory=time.time)


class Question(StrictModel):
    text: str
    answer: str = ""


class Decision(StrictModel):
    action: Literal["ask", "search", "guess", "hint", "clarify", "give_up"]
    text: str = Field(max_length=600)
    query: str = Field(max_length=600)
    character: str = Field(max_length=100)
    question_index: int = Field(ge=0, le=10000)


class Game(StrictModel):
    stream_id: str
    owner: str
    phase: Literal["planning", "answer", "guess", "hint", "conflict", "limit", "finished"] = "planning"
    questions: List[Question] = Field(default_factory=list)
    rejected: List[str] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    pending: str = ""
    conflict_index: int = 0
    kept_conflicts: List[int] = Field(default_factory=list)
    hint_used: bool = False
    hint: str = ""
    guesses: int = 0
    searches: int = 0
    limit: int = 20
    revision: int = 0
    opened_at: float = 0
    updated_at: float = Field(default_factory=time.time)
    seen_messages: List[str] = Field(default_factory=list)
    corrections: List[Dict[str, str]] = Field(default_factory=list)
    outcome: str = ""


def parse_object(text: str) -> Dict:
    """允许包裹 JSON 的代码块；不猜测或修补损坏的模型输出。"""
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    result = json.loads(clean)
    if not isinstance(result, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return result
