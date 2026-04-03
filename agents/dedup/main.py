"""Dedup Agent — 使用 Chroma 向量数据库对热点去重。

计算 title+content 的 embedding，与现有数据对比余弦相似度，
相似度 > 阈值则跳过（标记 skipped），否则存入 Chroma 并推进。
"""

from __future__ import annotations

import hashlib
import os
import sys

import chromadb
from chromadb.utils import embedding_functions

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
COLLECTION_NAME = "closeclaw_topics"
SIMILARITY_THRESHOLD = float(os.getenv("DEDUP_THRESHOLD", "0.92"))

QUEUE_NAME = "closeclaw.dedup"


def get_chroma_collection():
    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    # 使用轻量 sentence-transformers embedding（all-MiniLM-L6-v2）
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )


def is_duplicate(collection, text: str, doc_id: str) -> bool:
    """查询最近邻，若最高相似度超过阈值则认为重复。"""
    results = collection.query(
        query_texts=[text],
        n_results=1,
        include=["distances"],
    )
    distances = results.get("distances", [[]])[0]
    if not distances:
        return False
    # Chroma cosine distance：0=完全相同，1=完全不同
    similarity = 1.0 - distances[0]
    log.info("dedup similarity=%.4f threshold=%.4f doc_id=%s", similarity, SIMILARITY_THRESHOLD, doc_id)
    return similarity >= SIMILARITY_THRESHOLD


def handle(msg: StageMessage) -> None:
    log.info("dedup start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())
    if not job.topic:
        fail_job(msg.job_id, "no topic in job")
        return

    topic = job.topic
    text = f"{topic.title}\n{topic.content}"
    doc_id = hashlib.sha256(topic.url.encode()).hexdigest()

    try:
        collection = get_chroma_collection()
    except Exception as exc:
        fail_job(msg.job_id, f"chroma connection failed: {exc}")
        return

    try:
        if is_duplicate(collection, text, doc_id):
            log.info("topic duplicate, skipping job_id=%s url=%s", msg.job_id, topic.url)
            job.status = Status.SKIPPED
            job.stage = Stage.DEDUP
            # 不推进，直接静默丢弃
            return

        # 存入 Chroma
        collection.add(
            documents=[text],
            ids=[doc_id],
            metadatas=[{"url": topic.url, "job_id": msg.job_id}],
        )
    except Exception as exc:
        fail_job(msg.job_id, f"chroma error: {exc}")
        return

    job.stage = Stage.DEDUP
    job.status = Status.DONE
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
