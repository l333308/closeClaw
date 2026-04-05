"""运行固定热点样例，生成一批文案结果供人工验收。"""

from __future__ import annotations

import json
from pathlib import Path

from agents.writer.main import generate_copy
from shared.schema.job import AnalysisResult

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_FILE = ROOT / "examples" / "copy_quality_samples.json"
OUT_DIR = ROOT / "output" / "copy-validation"


def main() -> None:
    samples = json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    generated: list[dict] = []
    for sample in samples:
        analysis = AnalysisResult.from_dict(sample["analysis"])
        result = generate_copy(sample["title"], analysis)
        generated.append(
            {
                "id": sample["id"],
                "title": sample["title"],
                "copy": result.to_dict(),
            }
        )

    output_path = OUT_DIR / "latest.json"
    output_path.write_text(
        json.dumps(generated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"generated {len(generated)} copies -> {output_path}")


if __name__ == "__main__":
    main()
