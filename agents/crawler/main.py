"""Crawler Agent — 抓取 Twitter/Reddit AI 热点。

通过 MCP 工具（Tavily Search）抓取最新 AI 相关内容，
批量产出 HotTopic 列表，每条单独推进 pipeline。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import HotTopic, Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

TAVILY_URL = "https://api.tavily.com/search"

# 抓取关键词
CRAWL_QUERIES = [
    "AI artificial intelligence latest news",
    "large language model breakthrough",
    "OpenAI Anthropic Google AI update",
]

QUEUE_NAME = "closeclaw.crawl"


def crawl_topics() -> list[HotTopic]:
    """调用 Tavily API 抓取热点，返回去重后的 HotTopic 列表。"""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")

    topics: list[HotTopic] = []
    seen_urls: set[str] = set()

    for query in CRAWL_QUERIES:
        try:
            resp = requests.post(
                TAVILY_URL,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": False,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                url = item.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                topics.append(
                    HotTopic(
                        id=str(uuid.uuid4()),
                        source="tavily",
                        title=item.get("title", ""),
                        url=url,
                        content=item.get("content", "")[:2000],
                        score=item.get("score", 0.0),
                        created_at=datetime.utcnow(),
                    )
                )
        except Exception as exc:
            log.error("tavily query failed: query=%s err=%s", query, exc)

    return topics


def handle(msg: StageMessage) -> None:
    """处理 crawl 队列消息：抓取热点，每条 topic 推进各自的 Job。"""
    log.info("crawl start job_id=%s", msg.job_id)

    # 解析原始 Job（仅取 ID 用于基础信息）
    if msg.payload:
        try:
            base_job = Job.from_json(msg.payload.decode())
        except Exception:
            pass

    topics = crawl_topics()
    log.info("crawled %d topics", len(topics))

    if not topics:
        fail_job(msg.job_id, "no topics found")
        return

    for topic in topics:
        job = Job(
            id=str(uuid.uuid4()),
            stage=Stage.CRAWL,
            status=Status.DONE,
            trigger_job_id=msg.job_id,
            batch_topic_count=len(topics),
            topic=topic,
        )
        try:
            advance_job(job)
        except Exception as exc:
            log.error("advance failed topic=%s err=%s", topic.url, exc)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
