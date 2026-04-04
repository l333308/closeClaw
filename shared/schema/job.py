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

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "keywords": self.keywords,
            "sentiment": self.sentiment,
            "relevance": self.relevance,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisResult":
        return cls(**d)


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
    topic: Optional[HotTopic] = None
    analysis: Optional[AnalysisResult] = None
    copy: Optional[CopyResult] = None
    video: Optional[VideoResult] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "status": self.status.value,
            "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": self.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": self.error,
            "topic": self.topic.to_dict() if self.topic else None,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "copy": self.copy.to_dict() if self.copy else None,
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
            topic=HotTopic.from_dict(d["topic"]) if d.get("topic") else None,
            analysis=AnalysisResult.from_dict(d["analysis"]) if d.get("analysis") else None,
            copy=CopyResult.from_dict(d["copy"]) if d.get("copy") else None,
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
