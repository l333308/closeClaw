"""Publisher Agent — MVP 人工发布阶段。

生成视频后发送 Webhook 通知（企业微信/飞书/自定义），
操作人员收到通知后人工上传发布。
"""

from __future__ import annotations

import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

QUEUE_NAME = "closeclaw.publish"

# Webhook（企业微信/飞书机器人 URL，或自定义）
WEBHOOK_URL = os.getenv("PUBLISH_WEBHOOK_URL", "")

# 飞书 Webhook 格式（默认）
WEBHOOK_TYPE = os.getenv("WEBHOOK_TYPE", "feishu")  # feishu | wecom | custom


def send_notification(job: Job) -> bool:
    """发送发布通知，返回是否成功。"""
    if not WEBHOOK_URL:
        log.warning("PUBLISH_WEBHOOK_URL not set, printing to stdout")
        print_notification(job)
        return True

    payload = build_payload(job)
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("webhook sent job_id=%s", job.id)
        return True
    except Exception as exc:
        log.error("webhook failed: %s", exc)
        return False


def build_payload(job: Job) -> dict:
    """根据 Webhook 类型构建消息体。"""
    title = job.copy.title if job.copy else "未知标题"
    script = job.copy.script[:100] + "..." if job.copy and len(job.copy.script) > 100 else (job.copy.script if job.copy else "")
    hashtags = " ".join(f"#{t}" for t in (job.copy.hashtags or [])) if job.copy else ""
    video_path = job.video.file_path if job.video else "N/A"
    thumb_path = job.video.thumbnail_path if job.video else "N/A"
    duration = job.video.duration_sec if job.video else 0
    source_url = job.topic.url if job.topic else "N/A"

    if WEBHOOK_TYPE == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"📢 新视频待发布：{title}"}},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**脚本预览：**\n{script}"}},
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**话题标签：** {hashtags}"}},
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**视频路径：** `{video_path}`\n**封面：** `{thumb_path}`\n**时长：** {duration}s"}},
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**来源：** {source_url}"}},
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Job ID：** `{job.id}`"}},
                ],
            },
        }
    elif WEBHOOK_TYPE == "wecom":
        return {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"## 新视频待发布\n"
                    f"> **标题：** {title}\n"
                    f"> **脚本：** {script}\n"
                    f"> **话题：** {hashtags}\n"
                    f"> **视频：** {video_path}\n"
                    f"> **时长：** {duration}s\n"
                    f"> **来源：** {source_url}\n"
                    f"> **Job ID：** {job.id}"
                )
            },
        }
    else:
        return {
            "job_id": job.id,
            "title": title,
            "script": script,
            "hashtags": hashtags,
            "video_path": video_path,
            "thumbnail_path": thumb_path,
            "duration_sec": duration,
            "source_url": source_url,
        }


def print_notification(job: Job) -> None:
    """无 Webhook 时打印到 stdout。"""
    print("\n" + "=" * 60)
    print("【待发布视频通知】")
    print(f"Job ID    : {job.id}")
    print(f"标题      : {job.copy.title if job.copy else 'N/A'}")
    print(f"脚本      : {job.copy.script[:80] if job.copy else 'N/A'}...")
    print(f"话题      : {' '.join('#'+t for t in (job.copy.hashtags or []))}")
    print(f"视频路径  : {job.video.file_path if job.video else 'N/A'}")
    print(f"来源      : {job.topic.url if job.topic else 'N/A'}")
    print("=" * 60 + "\n")


def handle(msg: StageMessage) -> None:
    log.info("publish start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())

    ok = send_notification(job)
    if not ok:
        log.warning("notification failed but marking done job_id=%s", msg.job_id)

    job.stage = Stage.PUBLISH
    job.status = Status.DONE
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
