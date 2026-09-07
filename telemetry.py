"""Consent-based, privacy-limited installation registration."""

from __future__ import annotations

import json
import platform
import urllib.request


SUPABASE_URL = "https://donrddkzvrbkrnfgaveo.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_eNZyXZxPHJ4Xr-rZHWiHhA_I1B0FQF_"


def register_installation(installation_id: str, app_version: str, timeout: int = 6) -> bool:
    """Register a pseudonymous installation through an insert-only RPC."""
    payload = json.dumps(
        {
            "p_installation_id": installation_id,
            "p_app_version": app_version,
            "p_os_name": platform.system() or "Unknown",
            "p_os_version": platform.release() or "Unknown",
            "p_architecture": platform.machine() or "Unknown",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/register_device",
        data=payload,
        method="POST",
        headers={
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False
