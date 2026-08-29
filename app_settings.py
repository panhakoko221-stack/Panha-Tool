"""Small non-secret settings store for the desktop app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import uuid


DEFAULT_SETTINGS = {"microsoft_client_id": ""}


def load_settings(path: str | os.PathLike[str]) -> dict[str, str]:
    target = Path(path)
    if not target.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    result = dict(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        result["microsoft_client_id"] = str(data.get("microsoft_client_id", "")).strip()
    return result


def save_settings(path: str | os.PathLike[str], settings: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "microsoft_client_id": str(settings.get("microsoft_client_id", "")).strip(),
    }
    handle, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def valid_client_id(value: str) -> bool:
    try:
        uuid.UUID(value.strip())
        return True
    except (AttributeError, ValueError):
        return False
