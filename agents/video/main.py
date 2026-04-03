"""Video Agent — 使用 TTS + FFmpeg 合成短视频（9:16 竖屏）。

视频结构：
  ┌─────────────────────┐
  │  顶部品牌栏（AI头条）  │  0~140px
  ├─────────────────────┤
  │                     │
  │   标题（入场动画）    │  ~220px
  │                     │
  │   背景：动态渐变      │
  │   或 Pexels 素材     │
  │                     │
  ├─────────────────────┤
  │  字幕（同步高亮）     │  底部 300px
  ├─────────────────────┤
  │  进度条              │  底部 10px
  └─────────────────────┘

背景策略（优先级）：
  1. output/assets/ 目录下已有素材视频
  2. PEXELS_API_KEY 已配置 → 按话题关键词下载
  3. 内置动态渐变（geq 波浪动画，无需外部资源）
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import edge_tts
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import Job, Stage, Status, StageMessage, VideoResult
from agents.base import consume_queue, advance_job, fail_job, log

QUEUE_NAME = "closeclaw.video"

OUTPUT_DIR    = os.getenv("VIDEO_OUTPUT_DIR", "output/videos")
ASSETS_DIR    = os.getenv("VIDEO_ASSETS_DIR", "output/assets")
TTS_DIR       = os.getenv("VIDEO_TTS_DIR",    "output/tts")
TTS_VOICE     = os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")

VIDEO_WIDTH  = 1080
VIDEO_HEIGHT = 1920
ACCENT_COLOR = "0x6c63ff"   # 品牌紫
BG_COLOR     = "0x0a0e2e"   # 深海蓝（无素材时的背景底色）


# ── 字体 ─────────────────────────────────────────────────────────────────────

def find_chinese_font() -> str:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            log.info("using font: %s", path)
            return path
    log.warning("no Chinese font found; drawtext may show boxes")
    return ""


# ── TTS + SRT ─────────────────────────────────────────────────────────────────

async def _tts_with_subtitles(script: str, audio_path: str, srt_path: str) -> None:
    communicate = edge_tts.Communicate(script, TTS_VOICE)
    submaker    = edge_tts.SubMaker()
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(submaker.get_srt())
    log.info("tts done audio=%s srt=%s", audio_path, srt_path)


# ── 背景素材 ───────────────────────────────────────────────────────────────────

def get_local_asset() -> str | None:
    """返回 assets 目录中随机一个视频文件路径，没有则返回 None。"""
    assets = Path(ASSETS_DIR)
    if not assets.exists():
        return None
    videos = list(assets.glob("*.mp4")) + list(assets.glob("*.mov"))
    if not videos:
        return None
    import random
    return str(random.choice(videos))


def download_pexels_video(keywords: list[str]) -> str | None:
    """从 Pexels 按关键词搜索并下载第一个横屏视频，缓存到 assets。"""
    if not PEXELS_API_KEY:
        return None

    query = " ".join(keywords[:3])
    cache_key = hashlib.md5(query.encode()).hexdigest()[:8]
    cache_path = os.path.join(ASSETS_DIR, f"pexels_{cache_key}.mp4")

    if os.path.exists(cache_path):
        log.info("pexels cache hit: %s", cache_path)
        return cache_path

    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "orientation": "portrait", "per_page": 5},
            timeout=15,
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        if not videos:
            return None

        # 选时长最接近 60s 的素材
        best = min(videos, key=lambda v: abs(v.get("duration", 0) - 60))
        # 选分辨率最高的文件
        files = best.get("video_files", [])
        files = [f for f in files if f.get("quality") in ("hd", "sd")]
        if not files:
            return None
        files.sort(key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        video_url = files[0]["link"]

        log.info("downloading pexels video: %s", video_url)
        Path(ASSETS_DIR).mkdir(parents=True, exist_ok=True)
        with requests.get(video_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(cache_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        log.info("pexels video saved: %s", cache_path)
        return cache_path

    except Exception as exc:
        log.warning("pexels download failed: %s", exc)
        return None


def get_background_video(keywords: list[str]) -> str | None:
    """按优先级返回背景视频路径：本地素材 > Pexels > None（内置动画）。"""
    local = get_local_asset()
    if local:
        return local
    return download_pexels_video(keywords)


# ── 视频合成 ───────────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    return (
        text
        .replace("\\", "\\\\")
        .replace("'", "\u2019")
        .replace(":", "\\:")
        .replace("%", "\\%")
    )


def _build_filter(
    font: str,
    safe_title: str,
    srt_path: str,
    has_bg_video: bool,
    duration: float,
) -> tuple[str, str]:
    """
    构建 filter_complex 字符串，返回 (filter_complex, out_label)。
    """
    font_opt  = f":fontfile='{font}'" if font else ""
    font_name = Path(font).stem if font else "sans"

    has_srt = bool(srt_path and os.path.exists(srt_path) and os.path.getsize(srt_path) > 0)

    if has_bg_video:
        # 背景视频：缩放 + 裁剪适配竖屏
        bg = (
            f"[0:v]"
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
            f"format=yuv420p"
            f"[bg_raw];"
            # 叠加半透明暗化层，让文字更易读
            f"[bg_raw]"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:thickness=fill"
            f"[bg];"
        )
        audio_idx = "1"
    else:
        # 静态渐变背景（深海蓝底 + 中部浅色晕 + 顶/底暗角）
        # 用多层 drawbox 近似径向渐变，避免 geq 慢编码
        bg = (
            f"[0:v]"
            # 中间高亮区（模拟中心发光）
            f"drawbox=x=0:y=400:w={VIDEO_WIDTH}:h=1120:color=0x0f1a3e@0.9:thickness=fill,"
            # 顶部暗角
            f"drawbox=x=0:y=0:w={VIDEO_WIDTH}:h=350:color=0x050812@1:thickness=fill,"
            # 底部暗角
            f"drawbox=x=0:y=1570:w={VIDEO_WIDTH}:h=350:color=0x050812@1:thickness=fill,"
            # 左侧竖向渐变条（品牌紫）
            f"drawbox=x=0:y=0:w=8:h={VIDEO_HEIGHT}:color={ACCENT_COLOR}@0.8:thickness=fill"
            f"[bg];"
        )
        audio_idx = "1"

    # 顶部品牌栏（半透明黑底 + 品牌文字）
    brand = (
        f"[bg]"
        f"drawbox=x=0:y=0:w=iw:h=150:color=black@0.65:thickness=fill,"
        f"drawbox=x=0:y=150:w=iw:h=4:color={ACCENT_COLOR}@1:thickness=fill,"
        f"drawtext=text='AI 头条'{font_opt}:fontsize=52:fontcolor={ACCENT_COLOR}:"
        f"x=(w-text_w)/2:y=50"
        f"[branded];"
    )

    # 标题（前 0.4s 从屏幕外滑入）
    title = (
        f"[branded]"
        f"drawtext=text='{safe_title}'{font_opt}:"
        f"fontsize=64:fontcolor=white:"
        f"x=(w-text_w)/2:"
        f"y=if(gte(t\\,0.4)\\,220\\,220+(-0.4+t)*(-500)):"
        f"shadowcolor=black@0.9:shadowx=3:shadowy=3:"
        f"borderw=2:bordercolor=black@0.4"
        f"[titled];"
    )

    # 底部字幕区渐变遮罩
    sub_mask = (
        f"[titled]"
        f"drawbox=x=0:y={VIDEO_HEIGHT - 340}:w=iw:h=340:color=black@0.55:thickness=fill"
        f"[masked];"
    )

    # 进度条（底部 12px，随播放时间增长）
    if duration > 0:
        progress = (
            f"[masked]"
            f"drawbox=x=0:y={VIDEO_HEIGHT - 12}:w=iw:h=12:color=black@0.6:thickness=fill,"
            f"drawbox=x=0:y={VIDEO_HEIGHT - 12}:"
            f"w='iw*t/{duration:.1f}':h=12:color={ACCENT_COLOR}@0.9:thickness=fill"
            f"[progress];"
        )
        prev = "[progress]"
    else:
        progress = ""
        prev = "[masked]"

    # 字幕 subtitles 滤镜
    if has_srt:
        escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:")
        style = (
            f"FontName={font_name},"
            f"Fontsize=44,"
            f"PrimaryColour=&H00ffffff,"
            f"OutlineColour=&H00000000,"
            f"Outline=2,"
            f"Bold=1,"
            f"Alignment=2,"
            f"MarginV=100"
        )
        subs = f"{prev}subtitles='{escaped_srt}':force_style='{style}'[v]"
        out = "[v]"
    else:
        # 无字幕时直接输出
        subs = f"{prev}copy[v]"
        out = "[v]"

    fc = bg + brand + title + sub_mask + progress + subs
    return fc, out, audio_idx


def compose_video(
    audio_path: str,
    srt_path: str,
    output_path: str,
    title: str,
    keywords: list[str],
) -> int:
    font       = find_chinese_font()
    safe_title = _escape(title[:20])
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # 获取音频时长用于进度条
    try:
        from mutagen.mp3 import MP3
        audio_duration = MP3(audio_path).info.length
    except Exception:
        audio_duration = 0.0

    bg = get_background_video(keywords)

    if bg:
        inputs = ["-stream_loop", "-1", "-i", bg, "-i", audio_path]
        log.info("using background video: %s", bg)
    else:
        inputs = [
            "-f", "lavfi",
            "-i", f"color=c={BG_COLOR}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:r=30",
            "-i", audio_path,
        ]
        log.info("using built-in animated gradient background")

    fc, out, audio_idx = _build_filter(
        font, safe_title, srt_path, bool(bg), audio_duration
    )

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-shortest",
        "-filter_complex", fc,
        "-map", out,
        "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    log.info("ffmpeg start title='%s' bg=%s srt=%s", title[:20], bool(bg), bool(srt_path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-1000:]}")

    log.info("ffmpeg done: %s", output_path)
    return int(audio_duration) or 60


def _title_to_filename(title: str) -> str:
    """将标题转换为安全的文件名。
    - 中文字符限 10 个
    - 非中文字符限 20 个
    - 其余特殊字符替换为下划线
    """
    import re
    import unicodedata

    # 去掉控制字符和不可见字符
    title = "".join(c for c in title if unicodedata.category(c)[0] != "C")

    # 判断是否以中文为主
    chinese_chars = [c for c in title if "\u4e00" <= c <= "\u9fff"]
    if chinese_chars:
        # 保留前 10 个中文字符（含中文的整段截取）
        kept, count = [], 0
        for c in title:
            if "\u4e00" <= c <= "\u9fff":
                count += 1
            if count > 10:
                break
            kept.append(c)
        name = "".join(kept)
    else:
        name = title[:20]

    # 将文件系统不允许的字符替换为 _
    name = re.sub(r'[\\/*?:"<>|！？。，、；：""''【】（）《》…—\s]+', "_", name)
    name = name.strip("_")
    return name or "video"


def generate_thumbnail(video_path: str, thumb_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-ss", "00:00:02", "-vframes", "1", thumb_path]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log.warning("thumbnail generation failed")


# ── Handler ───────────────────────────────────────────────────────────────────

def handle(msg: StageMessage) -> None:
    log.info("video start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())
    if not job.copy:
        fail_job(msg.job_id, "no copy in job")
        return

    keywords    = job.analysis.keywords if job.analysis else []

    video_id    = str(uuid.uuid4())
    base_name   = _title_to_filename(job.copy.title)

    # 同名文件加序号后缀避免覆盖
    candidate   = os.path.join(OUTPUT_DIR, f"{base_name}.mp4")
    if os.path.exists(candidate):
        suffix = video_id[:6]
        base_name = f"{base_name}_{suffix}"

    audio_path  = os.path.join(TTS_DIR,    f"{video_id}.mp3")
    srt_path    = os.path.join(TTS_DIR,    f"{video_id}.srt")
    output_path = os.path.join(OUTPUT_DIR, f"{base_name}.mp4")
    thumb_path  = os.path.join(OUTPUT_DIR, f"{base_name}_thumb.jpg")

    Path(TTS_DIR).mkdir(parents=True, exist_ok=True)

    try:
        asyncio.run(_tts_with_subtitles(job.copy.script, audio_path, srt_path))
        duration = compose_video(audio_path, srt_path, output_path, job.copy.title, keywords)
        generate_thumbnail(output_path, thumb_path)
    except Exception as exc:
        fail_job(msg.job_id, f"video generation failed: {exc}")
        return

    job.stage = Stage.VIDEO
    job.status = Status.DONE
    job.video = VideoResult(
        file_path=output_path,
        duration_sec=duration,
        thumbnail_path=thumb_path,
    )
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
