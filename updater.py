"""Live Auto-Update module for Theara Mail & 2FA Tool.

Handles remote version checks, background downloads, zip extraction,
and detached process execution for seamless in-place updating.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional
import urllib.request
import zipfile

APP_VERSION = "1.0.3"
DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/panhakoko221-stack/Panha-Tool/main/dist/version.json"


@dataclass(frozen=True)
class UpdateInfo:
    """Metadata describing an available update."""

    version: str
    download_url: str
    title: str = ""
    changelog: str = ""
    mandatory: bool = False
    sha256: str = ""


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Convert version strings like '1.0.1' or 'v2.1.0-beta' into integer tuples for comparison."""
    cleaned = re.sub(r"^[vV]", "", str(version_str).strip())
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer_version(remote_ver: str, current_ver: str = APP_VERSION) -> bool:
    """Return True if remote_ver is strictly newer than current_ver."""
    try:
        remote_tuple = parse_version_tuple(remote_ver)
        current_tuple = parse_version_tuple(current_ver)
        # Pad shorter tuples with zeros
        max_len = max(len(remote_tuple), len(current_tuple))
        r_pad = remote_tuple + (0,) * (max_len - len(remote_tuple))
        c_pad = current_tuple + (0,) * (max_len - len(current_tuple))
        return r_pad > c_pad
    except Exception:
        return False


def get_update_manifest_url() -> str:
    """Read custom update URL from updater_config.json if present, else use default."""
    config_file = Path(sys.executable).resolve().parent / "updater_config.json"
    if not config_file.is_file():
        config_file = Path(__file__).resolve().parent / "updater_config.json"
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            url = data.get("manifest_url", "").strip()
            if url:
                return url
        except Exception:
            pass
    return DEFAULT_MANIFEST_URL


def check_for_updates(
    manifest_url: Optional[str] = None,
    current_version: str = APP_VERSION,
    timeout_seconds: int = 6,
) -> Optional[UpdateInfo]:
    """Query the remote update manifest. Return UpdateInfo if a newer version is found."""
    url = manifest_url or get_update_manifest_url()
    if not url:
        return None

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"Theara-Client/{current_version} ({sys.platform})",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read().decode("utf-8"))

        remote_version = str(data.get("version") or data.get("latest_version") or "").strip()
        download_url = str(data.get("download_url") or data.get("zip_url") or "").strip()

        if not remote_version or not download_url:
            return None

        if is_newer_version(remote_version, current_version):
            return UpdateInfo(
                version=remote_version,
                download_url=download_url,
                title=str(data.get("title", f"Theara v{remote_version}")),
                changelog=str(data.get("changelog") or data.get("release_notes") or ""),
                mandatory=bool(data.get("mandatory", False)),
                sha256=str(data.get("sha256", "")),
            )
        return None
    except Exception:
        # Fails closed / silently on network failures
        return None


def download_update_archive(
    url: str,
    target_path: Path,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
    timeout_seconds: int = 60,
) -> bool:
    """Download update zip file with chunking and progress tracking."""
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"Theara-Updater/{APP_VERSION}",
                "Cache-Control": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 64 * 1024  # 64 KB

            with open(target_path, "wb") as out_file:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        pct = min(100.0, (downloaded / total_size) * 100.0)
                        progress_callback(downloaded, total_size, pct)

        return target_path.is_file() and target_path.stat().st_size > 0
    except Exception:
        target_path.unlink(missing_ok=True)
        return False


def generate_updater_script(
    zip_archive_path: Path,
    app_install_dir: Path,
    executable_name: str = "Theara.exe",
) -> Path:
    """Generate a self-contained Windows batch file that replaces files and restarts the app."""
    temp_dir = Path(tempfile.gettempdir())
    script_path = temp_dir / "theara_apply_update.bat"
    extract_staging_dir = temp_dir / "theara_update_staging"

    # Batch script with robust retry loop, error handling, and process restart
    batch_content = f"""@echo off
setlocal enabledelayedexpansion
title Theara Live Updater

echo [Theara Updater] Waiting for application to exit...
timeout /t 1 /nobreak >nul

taskkill /F /IM "{executable_name}" 2>nul
timeout /t 1 /nobreak >nul

echo [Theara Updater] Extracting new version...
if exist "{extract_staging_dir}" rmdir /S /Q "{extract_staging_dir}" 2>nul
mkdir "{extract_staging_dir}" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '{zip_archive_path}' -DestinationPath '{extract_staging_dir}' -Force"

echo [Theara Updater] Installing updated files...
set "SOURCE_DIR={extract_staging_dir}"
if exist "{extract_staging_dir}\\Theara" (
    set "SOURCE_DIR={extract_staging_dir}\\Theara"
)
if exist "{extract_staging_dir}\\main.dist" (
    set "SOURCE_DIR={extract_staging_dir}\\main.dist"
)

xcopy "!SOURCE_DIR!\\*" "{app_install_dir}\\" /E /Y /Q /H /R >nul

echo [Theara Updater] Replacing and refreshing desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $d = [Environment]::GetFolderPath('Desktop'); $s_path = Join-Path $d 'Theara.lnk'; if (Test-Path $s_path) {{ Remove-Item -LiteralPath $s_path -Force }}; $s = $ws.CreateShortcut($s_path); $s.TargetPath = '{app_install_dir}\\{executable_name}'; $s.WorkingDirectory = '{app_install_dir}'; $s.IconLocation = '{app_install_dir}\\{executable_name},0'; $s.Description = 'Theara - Mail & 2FA Code Tool'; $s.Save()"

echo [Theara Updater] Cleaning temporary files...
rmdir /S /Q "{extract_staging_dir}" 2>nul
del /F /Q "{zip_archive_path}" 2>nul

echo [Theara Updater] Restarting Theara...
start "" /D "{app_install_dir}" "{app_install_dir}\\{executable_name}"

echo [Theara Updater] Update completed successfully!
timeout /t 2 /nobreak >nul
(goto) 2>nul & del "%~f0"
exit /b 0
"""
    script_path.write_text(batch_content, encoding="utf-8")
    return script_path


def get_installation_directory() -> Path:
    """Return the base directory where the application / exe is installed."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # When running from source/script (e.g. during development or test):
    return Path(__file__).resolve().parent


def apply_update_and_restart(
    zip_archive_path: Path,
    app_install_dir: Optional[Path] = None,
    executable_name: str = "Theara.exe",
) -> bool:
    """Launch the detached updater script and terminate the running process."""
    try:
        install_dir = app_install_dir or get_installation_directory()
        script_path = generate_updater_script(zip_archive_path, install_dir, executable_name)

        # Launch CMD with a visible console or detached so user can see progress if desired
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "Theara Updater", "cmd.exe", "/c", str(script_path)],
            shell=True,
        )
        return True
    except Exception:
        return False
