"""Generate version.json manifest for live client auto-updates."""

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def generate_manifest(
    version: str = "1.0.1",
    zip_url: str = "https://github.com/panhakoko221-stack/Panha-Tool/releases/latest/download/Theara.zip",
    changelog: str = "Live Auto-Update enabled, auto desktop shortcut, improved UI stability.",
    output_path: Path = PROJECT_ROOT / "dist" / "version.json",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": version,
        "title": f"Theara v{version}",
        "changelog": changelog,
        "download_url": zip_url,
        "mandatory": False,
    }
    output_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[OK] Generated update manifest at {output_path}")
    return output_path


if __name__ == "__main__":
    ver = sys.argv[1] if len(sys.argv) > 1 else "1.0.1"
    url = sys.argv[2] if len(sys.argv) > 2 else "https://github.com/panhakoko221-stack/Panha-Tool/releases/latest/download/Theara.zip"
    generate_manifest(ver, url)
