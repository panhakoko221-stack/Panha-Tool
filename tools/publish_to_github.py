"""1-Click GitHub Release & Update Publisher for Theara & all desktop tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "github_config.json"
DIST_DIR = PROJECT_ROOT / "dist"


def load_github_config() -> dict[str, str]:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "repo_owner": "panhakoko221-stack",
        "repo_name": "Panha-Tool",
        "github_token": os.environ.get("GITHUB_TOKEN", ""),
    }


def save_github_config(cfg: dict[str, str]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_git_credential() -> str:
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line.partition("=")[2].strip()
    return ""


def publish_github_release(
    version: str = "1.0.3",
    tag_name: str | None = None,
    release_title: str | None = None,
    changelog: str = "Simplified Yandex mail deletion, multi-account selection, and live deletion progress.",
) -> bool:
    cfg = load_github_config()
    owner = cfg.get("repo_owner", "").strip()
    repo = cfg.get("repo_name", "").strip()
    token = (
        cfg.get("github_token", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
        or load_git_credential()
    )

    if not owner or not repo:
        print("[Error] Please configure repo_owner and repo_name in github_config.json")
        return False

    tag = tag_name or f"v{version}"
    title = release_title or f"Theara v{version}"

    zip_file = DIST_DIR / "Theara.zip"
    version_file = DIST_DIR / "version.json"

    if not zip_file.is_file():
        print(f"[Error] Distributable zip not found at {zip_file}. Run Build script first.")
        return False

    print(f"Publishing release {tag} to https://github.com/{owner}/{repo} ...")

    # If no token is provided, guide the user on how to add token or use manual GitHub upload
    if not token:
        print("\n" + "=" * 60)
        print("GitHub Personal Access Token is needed for 1-click publishing.")
        print("1. Go to: https://github.com/settings/tokens (Fine-grained or classic)")
        print("2. Generate token with 'repo' / 'contents: write' permission.")
        print(f"3. Paste it into {CONFIG_PATH}")
        print("=" * 60 + "\n")
        print("Or you can simply upload the following files to your GitHub Release manually:")
        print(f"  - {zip_file}")
        print(f"  - {version_file}")
        return False

    try:
        # Step 1: Create Release via GitHub REST API
        release_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        payload = json.dumps({
            "tag_name": tag,
            "name": title,
            "body": changelog,
            "draft": False,
            "prerelease": False,
        }).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Theara-Publisher",
            "Content-Type": "application/json",
        }

        req = urllib.request.Request(release_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            rel_data = json.loads(resp.read().decode("utf-8"))

        upload_url = rel_data.get("upload_url", "").split("{")[0]
        html_url = rel_data.get("html_url", "")
        print(f"[OK] Created GitHub Release: {html_url}")

        # Step 2: Upload Theara.zip asset
        zip_bytes = zip_file.read_bytes()
        asset_url = f"{upload_url}?name=Theara.zip"
        asset_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Theara-Publisher",
            "Content-Type": "application/zip",
            "Content-Length": str(len(zip_bytes)),
        }
        asset_req = urllib.request.Request(asset_url, data=zip_bytes, headers=asset_headers, method="POST")
        with urllib.request.urlopen(asset_req, timeout=60) as resp:
            print("[OK] Uploaded Theara.zip asset successfully!")

        print(f"\n[OK] Successfully published {tag} live. Clients can now auto-update.")
        return True
    except Exception as exc:
        print(f"[Error] Failed to publish release: {exc}")
        return False


if __name__ == "__main__":
    v = sys.argv[1] if len(sys.argv) > 1 else "1.0.3"
    publish_github_release(v)
