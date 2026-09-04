from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import microsoft_oauth as oauth  # noqa: E402


def _crypt(data: bytes) -> bytes:
    """Reversible test cipher that makes plaintext-file assertions useful."""
    return bytes(value ^ 0xA5 for value in data)


class _FakeCache:
    def __init__(self) -> None:
        self.payload = json.dumps({"cache": "empty"})
        self.has_state_changed = False

    def serialize(self) -> str:
        self.has_state_changed = False
        return self.payload

    def deserialize(self, serialized: str) -> None:
        json.loads(serialized)
        self.payload = serialized
        self.has_state_changed = False


class _FakeApp:
    instances: list["_FakeApp"] = []

    def __init__(self, client_id: str, *, authority: str, token_cache: _FakeCache) -> None:
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache
        self.accounts: list[dict[str, str]] = []
        self.silent_result: object = None
        self.device_result: object = None
        self.flow_result: object = {
            "user_code": "ABCD-EFGH",
            "device_code": "opaque-device-value",
            "verification_uri": "https://microsoft.com/devicelogin",
        }
        self.account_queries: list[str] = []
        self.silent_calls: list[tuple[list[str], object]] = []
        self.flow_scopes: list[str] | None = None
        self.device_calls: list[tuple[dict[str, object], object]] = []
        self.removed: list[object] = []
        self.__class__.instances.append(self)

    def get_accounts(self, *, username: str):
        self.account_queries.append(username)
        return [
            account
            for account in self.accounts
            if account.get("username", "").casefold() == username.casefold()
        ]

    def acquire_token_silent(self, scopes, *, account):
        self.silent_calls.append((list(scopes), account))
        return self.silent_result

    def initiate_device_flow(self, *, scopes):
        self.flow_scopes = list(scopes)
        return self.flow_result

    def acquire_token_by_device_flow(self, flow, **kwargs):
        self.device_calls.append((dict(flow), kwargs.get("exit_condition")))
        if isinstance(self.device_result, Exception):
            raise self.device_result
        if isinstance(self.device_result, dict) and "access_token" in self.device_result:
            self.cache.payload = json.dumps(self.device_result)
            self.cache.has_state_changed = True
        return self.device_result

    def remove_account(self, account):
        self.removed.append(account)
        self.accounts.remove(account)
        self.cache.payload = json.dumps({"accounts": self.accounts})
        self.cache.has_state_changed = True


class _FakeMSAL:
    SerializableTokenCache = _FakeCache
    PublicClientApplication = _FakeApp


class MicrosoftOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeApp.instances = []
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cache_path = Path(self.folder.name) / "microsoft-token-cache.secure.json"
        patchers = (
            patch.object(oauth, "msal", _FakeMSAL),
            patch.object(oauth, "MSAL_AVAILABLE", True),
            patch.object(oauth, "protect_bytes", _crypt),
            patch.object(oauth, "unprotect_bytes", _crypt),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def manager(self) -> oauth.MicrosoftOAuthManager:
        return oauth.MicrosoftOAuthManager("public-client-id", self.cache_path)

    def test_device_flow_requests_only_outlook_imap_scope(self) -> None:
        manager = self.manager()
        flow = manager.initiate_device_flow()

        self.assertEqual(
            _FakeApp.instances[-1].flow_scopes,
            ["https://outlook.office.com/IMAP.AccessAsUser.All"],
        )
        self.assertEqual(flow["user_code"], "ABCD-EFGH")

    def test_silent_token_lookup_uses_matching_cached_account(self) -> None:
        manager = self.manager()
        app = _FakeApp.instances[-1]
        account = {"username": "Person@outlook.com"}
        app.accounts = [account]
        app.silent_result = {"access_token": "memory-only-access-token"}

        self.assertEqual(
            manager.get_access_token("person@outlook.com"),
            "memory-only-access-token",
        )
        self.assertEqual(app.account_queries, ["person@outlook.com"])
        self.assertEqual(
            app.silent_calls,
            [(["https://outlook.office.com/IMAP.AccessAsUser.All"], account)],
        )

        app.accounts = []
        self.assertEqual(manager.get_access_token("nobody@hotmail.com"), "")

    def test_success_is_sanitized_and_cache_file_contains_no_plaintext_token(self) -> None:
        manager = self.manager()
        app = _FakeApp.instances[-1]
        token = "highly-sensitive-access-token"
        app.device_result = {
            "access_token": token,
            "refresh_token": "also-sensitive",
            "id_token_claims": {"preferred_username": "person@outlook.com"},
        }

        result = manager.complete_device_flow({"user_code": "ABCD"})

        self.assertEqual(
            result,
            {
                "success": True,
                "email": "person@outlook.com",
                "detail": manager.SIGN_IN_COMPLETE,
            },
        )
        self.assertNotIn(token, repr(result))
        file_text = self.cache_path.read_text(encoding="utf-8")
        self.assertNotIn(token, file_text)
        self.assertNotIn("also-sensitive", file_text)
        envelope = json.loads(file_text)
        decrypted = _crypt(base64.b64decode(envelope["blob"])).decode("utf-8")
        self.assertIn(token, decrypted)

    def test_server_failures_and_exceptions_are_not_exposed(self) -> None:
        manager = self.manager()
        app = _FakeApp.instances[-1]
        secret_error = "AAD server detail containing secret-token-value"
        app.device_result = {
            "error": "authorization_declined",
            "error_description": secret_error,
        }
        result = manager.complete_device_flow({"user_code": "ABCD"})
        self.assertFalse(result["success"])
        self.assertEqual(result["detail"], manager.SIGN_IN_FAILED)
        self.assertNotIn(secret_error, repr(result))

        app.device_result = RuntimeError(secret_error)
        result = manager.complete_device_flow({"user_code": "ABCD"})
        self.assertEqual(result["detail"], manager.SIGN_IN_FAILED)
        self.assertNotIn(secret_error, repr(result))

    def test_cancellation_is_sanitized_and_passed_to_msal(self) -> None:
        manager = self.manager()
        app = _FakeApp.instances[-1]
        event = threading.Event()
        event.set()
        result = manager.complete_device_flow({"user_code": "ABCD"}, event)
        self.assertEqual(result["detail"], manager.SIGN_IN_CANCELLED)
        self.assertEqual(app.device_calls, [])

        event.clear()
        app.device_result = {"error": "expired_token"}
        manager.complete_device_flow({"user_code": "ABCD"}, event)
        exit_condition = app.device_calls[-1][1]
        self.assertTrue(callable(exit_condition))
        event.set()
        self.assertTrue(exit_condition({"expires_at": 1}))

    def test_missing_dependency_and_configuration_have_static_errors(self) -> None:
        with patch.object(oauth, "MSAL_AVAILABLE", False):
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth.MicrosoftOAuthManager("public-client-id", self.cache_path)
        self.assertEqual(str(caught.exception), oauth.OAuthError.DEPENDENCY_MISSING)

        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.MicrosoftOAuthManager("  ", self.cache_path)
        self.assertEqual(str(caught.exception), oauth.OAuthError.CONFIG_MISSING)
        self.assertIsNone(caught.exception.__cause__)

        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.MicrosoftOAuthManager("public-client-id", "")
        self.assertEqual(str(caught.exception), oauth.OAuthError.CONFIG_MISSING)

        unsafe = oauth.OAuthError("raw server error with token")
        self.assertEqual(str(unsafe), oauth.OAuthError.SERVICE_UNAVAILABLE)

    def test_sign_out_removes_account_and_persists_encrypted_cache(self) -> None:
        manager = self.manager()
        app = _FakeApp.instances[-1]
        account = {"username": "person@hotmail.com"}
        app.accounts = [account]

        self.assertTrue(manager.sign_out("Person@hotmail.com"))
        self.assertEqual(app.removed, [account])
        self.assertTrue(self.cache_path.exists())
        self.assertNotIn("person@hotmail.com", self.cache_path.read_text(encoding="utf-8"))
        self.assertFalse(manager.sign_out("missing@outlook.com"))

    def test_get_access_token_from_refresh_token_empty(self) -> None:
        token, error = oauth.get_access_token_from_refresh_token("")
        self.assertEqual(token, "")
        self.assertIn("empty", error.casefold())

    def test_get_access_token_from_refresh_token_mocked(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            import io
            mock_resp = io.BytesIO(b'{"access_token": "mock_token_123", "expires_in": 3600}')
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            token, error = oauth.get_access_token_from_refresh_token("valid_refresh_token", "my_client_id")
            self.assertEqual(token, "mock_token_123")
            self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
