"""Windows DPAPI-backed storage for mail accounts and app passwords.

The encrypted file can only be decrypted by the same Windows user account on
the same computer.  Plaintext passwords exist in memory only while the app is
running and are never written to logs.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Mapping


STORE_VERSION = 3
_SUPPORTED_STORE_VERSIONS = {1, 2, STORE_VERSION}
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecureStoreError(RuntimeError):
    """Raised when encrypted account data cannot be read or written."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _require_windows() -> None:
    if os.name != "nt":
        raise SecureStoreError("Secure account saving is available on Windows only.")


def _input_blob(data: bytes) -> tuple[_DataBlob, object]:
    # Keep the backing array alive until the Windows API call returns.
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _local_free(pointer: object) -> None:
    if not pointer:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))


def protect_bytes(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user with DPAPI."""
    _require_windows()
    source, source_buffer = _input_blob(data)
    destination = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "Yandex Code Fetcher accounts",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(destination),
    )
    del source_buffer
    if not ok:
        error = ctypes.get_last_error()
        raise SecureStoreError(f"Windows could not encrypt the account store (error {error}).")

    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        _local_free(destination.pbData)


def unprotect_bytes(data: bytes) -> bytes:
    """Decrypt bytes previously protected for the current Windows user."""
    _require_windows()
    source, source_buffer = _input_blob(data)
    destination = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(destination),
    )
    del source_buffer
    if not ok:
        error = ctypes.get_last_error()
        raise SecureStoreError(
            "This account store could not be decrypted by the current Windows user "
            f"(error {error})."
        )

    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        _local_free(destination.pbData)
        _local_free(description)


def _normalise_accounts(accounts: Iterable[Mapping[str, object]]) -> list[dict[str, str]]:
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for account in accounts:
        address = str(account.get("email", "")).strip()
        password = str(account.get("password", ""))
        provider = str(account.get("provider", "auto") or "auto").strip().casefold()
        imap_host = str(account.get("imap_host", "") or "").strip().casefold()
        mail_alias = str(account.get("mail_alias", "") or "").strip()
        key = address.casefold()
        password_optional = provider == "microsoft"
        if not address or (not password and not password_optional) or key in seen:
            continue
        seen.add(key)
        clean.append(
            {
                "email": address,
                "password": password,
                "provider": provider,
                "imap_host": imap_host,
                "mail_alias": mail_alias,
            }
        )
    return clean


def save_accounts(path: str | os.PathLike[str], accounts: Iterable[Mapping[str, object]]) -> None:
    """Atomically save accounts in a current-user encrypted JSON envelope."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": STORE_VERSION, "accounts": _normalise_accounts(accounts)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = {
        "version": STORE_VERSION,
        "storage": "windows-dpapi-current-user",
        "blob": base64.b64encode(protect_bytes(payload)).decode("ascii"),
    }

    handle, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(envelope, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_accounts(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Load and decrypt accounts, returning an empty list for a missing store."""
    target = Path(path)
    if not target.exists():
        return []
    try:
        envelope = json.loads(target.read_text(encoding="utf-8"))
        envelope_version = envelope.get("version")
        if envelope_version not in _SUPPORTED_STORE_VERSIONS:
            raise SecureStoreError("The account store uses an unsupported version.")
        encrypted = base64.b64decode(envelope["blob"], validate=True)
        payload = json.loads(unprotect_bytes(encrypted).decode("utf-8"))
        if (
            payload.get("version") not in _SUPPORTED_STORE_VERSIONS
            or not isinstance(payload.get("accounts"), list)
        ):
            raise SecureStoreError("The decrypted account store is invalid.")
        return _normalise_accounts(payload["accounts"])
    except SecureStoreError:
        raise
    except Exception as exc:
        raise SecureStoreError("The encrypted account store is damaged or unreadable.") from exc


def mask_password(password: str) -> str:
    """Return a non-revealing representation suitable for the table UI."""
    return "•" * min(max(len(password), 8), 16) if password else ""
