import base64
import json
import tempfile
import unittest
from pathlib import Path

from secure_store import load_accounts, mask_password, protect_bytes, save_accounts


@unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI test")
class SecureStoreTests(unittest.TestCase):
    def test_round_trip_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.secure.json"
            save_accounts(
                path,
                [
                    {"email": "Demo@yandex.com", "password": "fake-one"},
                    {"email": "demo@yandex.com", "password": "fake-two"},
                ],
            )
            self.assertEqual(
                load_accounts(path),
                [
                    {
                        "email": "Demo@yandex.com",
                        "password": "fake-one",
                        "provider": "auto",
                        "imap_host": "",
                        "mail_alias": "",
                    }
                ],
            )
            self.assertNotIn("fake-one", path.read_text(encoding="utf-8"))

    def test_loads_v1_store_and_adds_provider_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.secure.json"
            payload = json.dumps(
                {
                    "version": 1,
                    "accounts": [{"email": "legacy@yandex.com", "password": "fake-old"}],
                }
            ).encode("utf-8")
            envelope = {
                "version": 1,
                "storage": "windows-dpapi-current-user",
                "blob": base64.b64encode(protect_bytes(payload)).decode("ascii"),
            }
            path.write_text(json.dumps(envelope), encoding="utf-8")
            self.assertEqual(load_accounts(path)[0]["provider"], "auto")
            self.assertEqual(load_accounts(path)[0]["mail_alias"], "")

    def test_passwordless_microsoft_profile_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.secure.json"
            save_accounts(
                path,
                [
                    {
                        "email": "demo@outlook.com",
                        "password": "",
                        "provider": "microsoft",
                        "imap_host": "",
                    }
                ],
            )
            self.assertEqual(load_accounts(path)[0]["provider"], "microsoft")

    def test_loads_v2_store_and_adds_empty_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.secure.json"
            payload = json.dumps(
                {
                    "version": 2,
                    "accounts": [
                        {
                            "email": "legacy@zoho.com",
                            "password": "fake-old",
                            "provider": "zoho",
                            "imap_host": "imap.zoho.com",
                        }
                    ],
                }
            ).encode("utf-8")
            envelope = {
                "version": 2,
                "storage": "windows-dpapi-current-user",
                "blob": base64.b64encode(protect_bytes(payload)).decode("ascii"),
            }
            path.write_text(json.dumps(envelope), encoding="utf-8")
            loaded = load_accounts(path)[0]
            self.assertEqual(loaded["provider"], "zoho")
            self.assertEqual(loaded["mail_alias"], "")

    def test_alias_round_trip_is_encrypted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.secure.json"
            alias = "demo+100teamA@yandex.com"
            save_accounts(
                path,
                [
                    {
                        "email": "demo@yandex.com",
                        "password": "fake-secret",
                        "provider": "yandex",
                        "imap_host": "",
                        "mail_alias": alias,
                    }
                ],
            )
            self.assertEqual(load_accounts(path)[0]["mail_alias"], alias)
            encrypted_text = path.read_text(encoding="utf-8")
            self.assertNotIn(alias, encrypted_text)
            self.assertNotIn("fake-secret", encrypted_text)

    def test_mask_never_reveals_password(self):
        password = "example-secret"
        masked = mask_password(password)
        self.assertNotIn(password, masked)
        self.assertTrue(masked)


if __name__ == "__main__":
    unittest.main()
