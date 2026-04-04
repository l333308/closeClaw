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
DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
LOCAL_MODEL_PATH = os.path.join(os.getcwd(), "models", "all-MiniLM-L6-v2")
EMBEDDING_MODEL_NAME = os.getenv("DEDUP_MODEL_NAME", DEFAULT_MODEL_NAME)
EMBEDDING_MODEL_PATH = os.getenv("DEDUP_MODEL_PATH", LOCAL_MODEL_PATH)
HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")
HF_HOME = os.getenv("HF_HOME", os.path.join(os.getcwd(), ".cache", "huggingface"))
TRANSFORMERS_CACHE = os.getenv("TRANSFORMERS_CACHE", os.path.join(HF_HOME, "transformers"))
DEFAULT_HTTP_PROXY = os.getenv("DEDUP_HTTP_PROXY", "http://127.0.0.1:7890")
DEFAULT_ALL_PROXY = os.getenv("DEDUP_ALL_PROXY", "socks5://127.0.0.1:7890")

QUEUE_NAME = "closeclaw.dedup"

os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)
os.environ.setdefault("HF_HOME", HF_HOME)
os.environ.setdefault("TRANSFORMERS_CACHE", TRANSFORMERS_CACHE)
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", os.getenv("HF_HUB_DOWNLOAD_TIMEOUT", "60"))
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", os.getenv("HF_HUB_ETAG_TIMEOUT", "30"))
os.environ.setdefault("HTTP_PROXY", os.getenv("HTTP_PROXY", DEFAULT_HTTP_PROXY))
os.environ.setdefault("HTTPS_PROXY", os.getenv("HTTPS_PROXY", DEFAULT_HTTP_PROXY))
os.environ.setdefault("ALL_PROXY", os.getenv("ALL_PROXY", DEFAULT_ALL_PROXY))
os.environ.setdefault("http_proxy", os.getenv("http_proxy", os.environ["HTTP_PROXY"]))
os.environ.setdefault("https_proxy", os.getenv("https_proxy", os.environ["HTTPS_PROXY"]))
os.environ.setdefault("all_proxy", os.getenv("all_proxy", os.environ["ALL_PROXY"]))
os.environ.setdefault("NO_PROXY", os.getenv("NO_PROXY", "localhost,127.0.0.1,::1"))
os.environ.setdefault("no_proxy", os.getenv("no_proxy", os.environ["NO_PROXY"]))
os.makedirs(TRANSFORMERS_CACHE, exist_ok=True)


def get_embedding_model_ref() -> str:
    """优先使用仓库内模型目录，缺失时回退到远端模型名。"""
    if os.path.isdir(EMBEDDING_MODEL_PATH):
        log.info("using local embedding model: %s", EMBEDDING_MODEL_PATH)
        return EMBEDDING_MODEL_PATH
    log.info("using remote embedding model: %s", EMBEDDING_MODEL_NAME)
    return EMBEDDING_MODEL_NAME


def get_chroma_collection():
    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    # 使用轻量 sentence-transformers embedding（all-MiniLM-L6-v2）
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=get_embedding_model_ref(),
        cache_folder=TRANSFORMERS_CACHE,
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
