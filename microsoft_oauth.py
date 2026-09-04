"""Microsoft OAuth support for Outlook/Hotmail IMAP access.

The MSAL token cache is encrypted for the current Windows user before it is
written to disk.  This module deliberately keeps all user-facing failures
static so that tokens and identity-provider error responses are never leaked.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping

from secure_store import protect_bytes, unprotect_bytes

try:  # MSAL is optional so the rest of the application can still start.
    import msal  # type: ignore
except ImportError:  # pragma: no cover - exercised by reloading in tests
    msal = None  # type: ignore[assignment]
    MSAL_AVAILABLE = False
else:
    MSAL_AVAILABLE = True


IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
_SCOPES = [IMAP_SCOPE]
_CACHE_VERSION = 1
_CACHE_STORAGE = "windows-dpapi-current-user"


class OAuthError(RuntimeError):
    """An OAuth failure whose text is always safe to display to a user."""

    DEPENDENCY_MISSING = "Microsoft sign-in support is not installed."
    CONFIG_MISSING = "Microsoft sign-in is not configured."
    CACHE_UNAVAILABLE = "Microsoft sign-in data could not be loaded or saved."
    SERVICE_UNAVAILABLE = "Microsoft sign-in is currently unavailable."
    TOKEN_UNAVAILABLE = "A Microsoft access token could not be obtained."

    _SAFE_MESSAGES = frozenset(
        {
            DEPENDENCY_MISSING,
            CONFIG_MISSING,
            CACHE_UNAVAILABLE,
            SERVICE_UNAVAILABLE,
            TOKEN_UNAVAILABLE,
        }
    )

    def __init__(self, message: str = SERVICE_UNAVAILABLE) -> None:
        # Do not allow an upstream exception or server response to become the
        # exception's printable representation by accident.
        safe_message = message if message in self._SAFE_MESSAGES else self.SERVICE_UNAVAILABLE
        super().__init__(safe_message)


class MicrosoftOAuthManager:
    """Manage a DPAPI-protected MSAL cache for Microsoft consumer accounts."""

    SIGN_IN_COMPLETE = "Microsoft sign-in completed."
    SIGN_IN_CANCELLED = "Microsoft sign-in was cancelled."
    SIGN_IN_FAILED = "Microsoft sign-in could not be completed."

    def __init__(
        self,
        client_id: str,
        cache_path: str | os.PathLike[str],
        authority: str = "https://login.microsoftonline.com/consumers",
    ) -> None:
        if not MSAL_AVAILABLE or msal is None:
            raise OAuthError(OAuthError.DEPENDENCY_MISSING)
        if not isinstance(client_id, str) or not client_id.strip():
            raise OAuthError(OAuthError.CONFIG_MISSING)
        if not isinstance(authority, str) or not authority.strip():
            raise OAuthError(OAuthError.CONFIG_MISSING)
        if isinstance(cache_path, str) and not cache_path.strip():
            raise OAuthError(OAuthError.CONFIG_MISSING)
        try:
            self._cache_path = Path(cache_path)
            if not self._cache_path.name:
                raise ValueError("cache path must name a file")
        except (TypeError, ValueError, OSError):
            raise OAuthError(OAuthError.CONFIG_MISSING) from None

        self._lock = threading.RLock()
        try:
            self._cache = msal.SerializableTokenCache()
            self._load_cache()
            self._app = msal.PublicClientApplication(
                client_id=client_id.strip(),
                authority=authority.strip(),
                token_cache=self._cache,
            )
        except OAuthError:
            raise
        except Exception:
            raise OAuthError(OAuthError.SERVICE_UNAVAILABLE) from None

    def _load_cache(self) -> None:
        """Load and decrypt the cache.  Caller constructs before publication."""
        if not self._cache_path.exists():
            return
        try:
            envelope = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if (
                not isinstance(envelope, dict)
                or envelope.get("version") != _CACHE_VERSION
                or envelope.get("storage") != _CACHE_STORAGE
            ):
                raise ValueError("invalid envelope")
            encrypted = base64.b64decode(envelope["blob"], validate=True)
            serialized = unprotect_bytes(encrypted).decode("utf-8")
            # SerializableTokenCache validates its own JSON representation.
            self._cache.deserialize(serialized)
        except Exception:
            raise OAuthError(OAuthError.CACHE_UNAVAILABLE) from None

    def _save_cache(self, *, force: bool = False) -> None:
        """Atomically persist the cache while the manager lock is held."""
        if not force and not bool(getattr(self._cache, "has_state_changed", False)):
            return

        target = self._cache_path
        temp_name: str | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            serialized = self._cache.serialize().encode("utf-8")
            envelope = {
                "version": _CACHE_VERSION,
                "storage": _CACHE_STORAGE,
                "blob": base64.b64encode(protect_bytes(serialized)).decode("ascii"),
            }
            handle, temp_name = tempfile.mkstemp(
                prefix=target.name + ".",
                suffix=".tmp",
                dir=target.parent,
            )
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(envelope, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
            temp_name = None
        except Exception:
            # ``serialize`` clears MSAL's change flag.  Restore it when any
            # later encryption or filesystem step fails so a retry can save.
            try:
                self._cache.has_state_changed = True
            except Exception:
                pass
            raise OAuthError(OAuthError.CACHE_UNAVAILABLE) from None
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    @staticmethod
    def _account_email(account: object) -> str:
        if not isinstance(account, Mapping):
            return ""
        value = account.get("username", "")
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _matching_accounts(cls, accounts: object, email: str) -> list[Mapping[str, Any]]:
        if not isinstance(accounts, (list, tuple)):
            return []
        wanted = email.casefold()
        matches: list[Mapping[str, Any]] = []
        for account in accounts:
            if not isinstance(account, Mapping):
                continue
            account_email = cls._account_email(account)
            # MSAL's username filter already limits accounts.  Keeping an
            # account with no username supports older/minimal cache records.
            if not account_email or account_email.casefold() == wanted:
                matches.append(account)
        return matches

    @staticmethod
    def _result_email(result: object) -> str:
        if not isinstance(result, Mapping):
            return ""
        claims = result.get("id_token_claims")
        if not isinstance(claims, Mapping):
            return ""
        for key in ("preferred_username", "email", "upn"):
            value = claims.get(key)
            if isinstance(value, str):
                candidate = value.strip()
                if candidate and "@" in candidate and not any(ch in candidate for ch in "\r\n"):
                    return candidate
        return ""

    def get_access_token(self, email: str) -> str:
        """Return a silently acquired cached token, or an empty string."""
        address = email.strip() if isinstance(email, str) else ""
        if not address:
            return ""

        with self._lock:
            try:
                accounts = self._matching_accounts(
                    self._app.get_accounts(username=address),
                    address,
                )
                if not accounts:
                    return ""
                result = self._app.acquire_token_silent(_SCOPES, account=accounts[0])
                self._save_cache()
                if not isinstance(result, Mapping):
                    return ""
                token = result.get("access_token")
                return token if isinstance(token, str) and token else ""
            except OAuthError:
                raise
            except Exception:
                raise OAuthError(OAuthError.TOKEN_UNAVAILABLE) from None

    def initiate_device_flow(self) -> dict[str, Any]:
        """Start Microsoft's device-code flow for Outlook IMAP access."""
        with self._lock:
            try:
                flow = self._app.initiate_device_flow(scopes=_SCOPES)
                if not isinstance(flow, dict) or not flow.get("user_code"):
                    raise OAuthError(OAuthError.SERVICE_UNAVAILABLE)
                return dict(flow)
            except OAuthError:
                raise
            except Exception:
                raise OAuthError(OAuthError.SERVICE_UNAVAILABLE) from None

    def complete_device_flow(
        self,
        flow: Mapping[str, Any],
        cancel_event: object | None = None,
    ) -> dict[str, object]:
        """Wait for device sign-in and return only sanitized status fields."""
        cancelled = getattr(cancel_event, "is_set", None)
        is_cancelled = cancelled if callable(cancelled) else lambda: False
        if is_cancelled():
            return {"success": False, "email": "", "detail": self.SIGN_IN_CANCELLED}
        if not isinstance(flow, Mapping):
            return {"success": False, "email": "", "detail": self.SIGN_IN_FAILED}

        with self._lock:
            try:
                kwargs: dict[str, Any] = {}
                if cancel_event is not None and callable(cancelled):
                    # MSAL passes the current flow to this callback.
                    kwargs["exit_condition"] = lambda *_flow: is_cancelled()
                result = self._app.acquire_token_by_device_flow(dict(flow), **kwargs)
                if (
                    isinstance(result, Mapping)
                    and isinstance(result.get("access_token"), str)
                    and bool(result.get("access_token"))
                ):
                    # A successful interactive sign-in is important enough to
                    # persist even when a test double or future cache backend
                    # does not expose MSAL's change flag.
                    self._save_cache(force=True)
                    return {
                        "success": True,
                        "email": self._result_email(result),
                        "detail": self.SIGN_IN_COMPLETE,
                    }
                detail = self.SIGN_IN_CANCELLED if is_cancelled() else self.SIGN_IN_FAILED
                return {"success": False, "email": "", "detail": detail}
            except Exception:
                # Neither exception text nor an MSAL error_description is safe
                # to surface here.
                return {"success": False, "email": "", "detail": self.SIGN_IN_FAILED}

    def sign_out(self, email: str) -> bool:
        """Remove cached tokens for an account and persist that removal."""
        address = email.strip() if isinstance(email, str) else ""
        if not address:
            return False
        with self._lock:
            try:
                accounts = self._matching_accounts(
                    self._app.get_accounts(username=address),
                    address,
                )
                if not accounts:
                    return False
                for account in accounts:
                    self._app.remove_account(account)
                self._save_cache(force=True)
                return True
            except Exception:
                return False


def get_access_token_from_refresh_token(
    refresh_token: str,
    client_id: str = "",
    timeout: int = 15,
) -> tuple[str, str]:
    """Exchange a Microsoft OAuth2 refresh_token for an access_token.
    Returns (access_token, error_message).
    """
    import urllib.parse
    import urllib.request
    import urllib.error

    token = str(refresh_token or "").strip()
    if not token:
        return "", "Refresh token is empty."

    cid = str(client_id or "").strip() or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

    endpoints = [
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "https://login.live.com/oauth20_token.srf",
    ]

    last_error = ""
    for url in endpoints:
        data = urllib.parse.urlencode({
            "client_id": cid,
            "grant_type": "refresh_token",
            "refresh_token": token,
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res_json = json.loads(resp.read().decode("utf-8", errors="ignore"))
                access_token = res_json.get("access_token", "")
                if access_token:
                    return access_token, ""
                last_error = str(res_json.get("error_description", res_json.get("error", "OAuth failed")))
        except urllib.error.HTTPError as err:
            try:
                err_body = err.read().decode("utf-8", errors="ignore")
                err_json = json.loads(err_body)
                last_error = err_json.get("error_description", err_json.get("error", str(err)))
            except Exception:
                last_error = f"HTTP {err.code}: {err.reason}"
        except Exception as err:
            last_error = str(err)

    return "", last_error or "Could not acquire access token from refresh token."


__all__ = [
    "MSAL_AVAILABLE",
    "OAuthError",
    "MicrosoftOAuthManager",
    "get_access_token_from_refresh_token",
]
