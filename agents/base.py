"""共享 Agent 工具函数：RabbitMQ 消费/发布、Orchestrator 回调。"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Callable

from dotenv import load_dotenv
import pika
import requests

# 加载项目根目录的 .env
_root = os.path.join(os.path.dirname(__file__), "../")
load_dotenv(os.path.join(_root, ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import Job, Stage, Status, StageMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("agent")

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://closeclaw:closeclaw@localhost:5672/")


def get_rabbitmq_connection() -> pika.BlockingConnection:
    params = pika.URLParameters(RABBITMQ_URL)
    params.heartbeat = 600  # 10分钟，兼容视频编码等长任务
    return pika.BlockingConnection(params)


def consume_queue(queue_name: str, handler: Callable[[StageMessage], None]) -> None:
    """阻塞消费指定队列，连接断开后自动重连。"""
    retry_delay = 5

    while True:
        try:
            connection = get_rabbitmq_connection()
            channel = connection.channel()
            channel.queue_declare(queue=queue_name, durable=True)
            channel.basic_qos(prefetch_count=1)

            def on_message(ch, method, _props, body):
                try:
                    msg = StageMessage.from_json(body.decode())
                    handler(msg)
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as exc:
                    log.exception("handler error: %s", exc)
                    try:
                        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    except Exception:
                        pass

            channel.basic_consume(queue=queue_name, on_message_callback=on_message)
            log.info("consuming queue=%s", queue_name)
            channel.start_consuming()

        except KeyboardInterrupt:
            log.info("agent stopping (KeyboardInterrupt)")
            break
        except Exception as exc:
            log.warning("connection lost (%s), reconnecting in %ds...", exc, retry_delay)
            try:
                connection.close()
            except Exception:
                pass
            import time
            time.sleep(retry_delay)


def advance_job(job: Job) -> None:
    """通知 Orchestrator 当前阶段完成，推进到下一阶段。"""
    url = f"{ORCHESTRATOR_URL}/jobs/{job.id}/advance"
    resp = requests.post(url, json=job.to_dict(), timeout=10)
    resp.raise_for_status()
    log.info("advanced job=%s stage=%s", job.id, job.stage)


def fail_job(job_id: str, reason: str) -> None:
    """通知 Orchestrator 当前阶段失败。"""
    url = f"{ORCHESTRATOR_URL}/jobs/{job_id}/fail"
    requests.post(url, json={"reason": reason}, timeout=10)
    log.error("failed job=%s reason=%s", job_id, reason)
