"""共享消息 schema，与 Go 端 shared/schema/job.go 保持一致。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import base64
import json


def _parse_datetime(value: str) -> datetime:
    """兼容 Go/Python 间小数秒位数不一致的 ISO 时间。"""
    value = value.rstrip("Z")
    if "." not in value:
        return datetime.fromisoformat(value)

    main, frac = value.split(".", 1)
    frac = "".join(ch for ch in frac if ch.isdigit())
    frac = (frac + "000000")[:6]
    return datetime.fromisoformat(f"{main}.{frac}")


class Stage(str, Enum):
    CRAWL = "crawl"
    DEDUP = "dedup"
    ANALYZE = "analyze"
    WRITE = "write"
    REVIEW = "review"
    VIDEO = "video"
    PUBLISH = "publish"


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class HotTopic:
    id: str
    source: str  # "twitter" | "reddit"
    title: str
    url: str
    content: str
    score: float
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
            "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HotTopic":
        d = dict(d)
        d["created_at"] = _parse_datetime(d["created_at"])
        return cls(**d)


@dataclass
class AnalysisResult:
    summary: str
    keywords: list[str]
    sentiment: str
    relevance: float
    core_point: str = ""
    why_it_matters: str = ""
    impact_on_people: str = ""
    stance_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "keywords": self.keywords,
            "sentiment": self.sentiment,
            "relevance": self.relevance,
            "core_point": self.core_point,
            "why_it_matters": self.why_it_matters,
            "impact_on_people": self.impact_on_people,
            "stance_hint": self.stance_hint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisResult":
        return cls(
            summary=d["summary"],
            keywords=d["keywords"],
            sentiment=d["sentiment"],
            relevance=float(d["relevance"]),
            core_point=d.get("core_point", ""),
            why_it_matters=d.get("why_it_matters", ""),
            impact_on_people=d.get("impact_on_people", ""),
            stance_hint=d.get("stance_hint", ""),
        )


@dataclass
class CopyResult:
    title: str
    script: str
    hashtags: list[str]

    def to_dict(self) -> dict:
        return {"title": self.title, "script": self.script, "hashtags": self.hashtags}

    @classmethod
    def from_dict(cls, d: dict) -> "CopyResult":
        return cls(**d)


@dataclass
class CopyReviewResult:
    attraction: int
    emotion: int
    information_density: int
    virality: int
    verdict: str
    summary: str
    suggestions: list[str]

    def to_dict(self) -> dict:
        return {
            "attraction": self.attraction,
            "emotion": self.emotion,
            "information_density": self.information_density,
            "virality": self.virality,
            "verdict": self.verdict,
            "summary": self.summary,
            "suggestions": self.suggestions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CopyReviewResult":
        return cls(
            attraction=int(d["attraction"]),
            emotion=int(d["emotion"]),
            information_density=int(d["information_density"]),
            virality=int(d["virality"]),
            verdict=d["verdict"],
            summary=d.get("summary", ""),
            suggestions=d.get("suggestions", []),
        )


@dataclass
class VideoResult:
    file_path: str
    duration_sec: int
    thumbnail_path: str

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "duration_sec": self.duration_sec,
            "thumbnail_path": self.thumbnail_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VideoResult":
        return cls(**d)


@dataclass
class Job:
    id: str
    stage: Stage
    status: Status
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    error: str = ""
    copy_rewrite_count: int = 0
    topic: Optional[HotTopic] = None
    analysis: Optional[AnalysisResult] = None
    copy: Optional[CopyResult] = None
    review: Optional[CopyReviewResult] = None
    video: Optional[VideoResult] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "status": self.status.value,
            "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": self.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": self.error,
            "copy_rewrite_count": self.copy_rewrite_count,
            "topic": self.topic.to_dict() if self.topic else None,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "copy": self.copy.to_dict() if self.copy else None,
            "review": self.review.to_dict() if self.review else None,
            "video": self.video.to_dict() if self.video else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            id=d["id"],
            stage=Stage(d["stage"]),
            status=Status(d["status"]),
            created_at=_parse_datetime(d["created_at"]),
            updated_at=_parse_datetime(d["updated_at"]),
            error=d.get("error", ""),
            copy_rewrite_count=d.get("copy_rewrite_count", 0),
            topic=HotTopic.from_dict(d["topic"]) if d.get("topic") else None,
            analysis=AnalysisResult.from_dict(d["analysis"]) if d.get("analysis") else None,
            copy=CopyResult.from_dict(d["copy"]) if d.get("copy") else None,
            review=CopyReviewResult.from_dict(d["review"]) if d.get("review") else None,
            video=VideoResult.from_dict(d["video"]) if d.get("video") else None,
        )

    @classmethod
    def from_json(cls, s: str) -> "Job":
        return cls.from_dict(json.loads(s))


@dataclass
class StageMessage:
    job_id: str
    stage: Stage
    payload: bytes

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "stage": self.stage.value,
            # Python → Go: base64 编码（与 Go []byte JSON 序列化行为一致）
            "payload": base64.b64encode(self.payload).decode("ascii") if self.payload else "",
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "StageMessage":
        d = json.loads(s)
        raw = d.get("payload") or ""
        # Go []byte → JSON 是 base64 字符串，需要 b64decode
        payload = base64.b64decode(raw) if raw else b""
        return cls(
            job_id=d["job_id"],
            stage=Stage(d["stage"]),
            payload=payload,
        )
