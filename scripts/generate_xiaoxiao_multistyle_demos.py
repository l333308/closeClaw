from __future__ import annotations

import asyncio
from pathlib import Path

from mutagen.mp3 import MP3

import edge_tts

VOICE = "zh-CN-XiaoxiaoNeural"
OUT_DIR = Path("output/voice-demos")

DEMOS = [
    ("11_xiaoxiao_cheerful", "cheerful", "+10%", "+2Hz", "大家好，我是晓晓。今天心情特别好，AI workflow 跑通啦，Hello world！"),
    ("12_xiaoxiao_gentle", "gentle", "-8%", "-8Hz", "大家好，我是晓晓。接下来我会温柔地介绍这个项目，让内容更自然、更好听。"),
    ("13_xiaoxiao_chat", "chat", "+4%", "+0Hz", "嗨，今天我们轻松聊聊 AI agent、prompt engineering，还有这个小项目的日常使用体验。"),
    ("14_xiaoxiao_customerservice", "customerservice", "-2%", "-2Hz", "您好，这里是 CloseClaw 智能助手。您的请求已经收到，稍后我会为您继续处理。"),
    ("15_xiaoxiao_newscast", "newscast", "+0%", "-4Hz", "现在播报一条 AI 快讯。新模型上线后，开发效率明显提升，社区反响热烈。"),
    ("16_xiaoxiao_hopeful", "hopeful", "+6%", "+6Hz", "未来每个人都能拥有自己的 AI 助手。Keep building，更多可能，正在发生。"),
]

async def synthesize_with_style(text: str, rate: str, pitch: str, out_path: Path) -> None:
    communicate = edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch)
    await communicate.save(str(out_path))


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "## Xiaoxiao Multi-Style Demos",
        "",
        "Voice: 晓晓-多情感表达 | `zh-CN-XiaoxiaoNeural`",
        "Note: `edge-tts` 当前接口无法直接导出微软官网 `express-as` 的官方情感风格，以下为基于同一音色通过文案、语速、音高生成的近似场景 demo。",
        "",
    ]

    for filename, style, rate, pitch, text in DEMOS:
        out_path = OUT_DIR / f"{filename}.mp3"
        print(f"generating {filename} ({style})")
        await synthesize_with_style(text, rate, pitch, out_path)
        duration = MP3(out_path).info.length
        lines.append(f"- {filename}.mp3 | scene=`{style}` | rate=`{rate}` | pitch=`{pitch}` | {duration:.1f}s")

    readme = OUT_DIR / "README.md"
    existing = readme.read_text(encoding="utf-8") if readme.exists() else ""
    content = existing.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    readme.write_text(content, encoding="utf-8")
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
