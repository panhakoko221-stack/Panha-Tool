import tempfile
import unittest
from pathlib import Path

from app_settings import load_settings, save_settings, valid_client_id


class AppSettingsTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            save_settings(path, {"microsoft_client_id": "11111111-1111-1111-1111-111111111111"})
            self.assertEqual(
                load_settings(path)["microsoft_client_id"],
                "11111111-1111-1111-1111-111111111111",
            )

    def test_invalid_or_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            self.assertEqual(load_settings(path)["microsoft_client_id"], "")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_settings(path)["microsoft_client_id"], "")

    def test_client_id_validation(self):
        self.assertTrue(valid_client_id("11111111-1111-1111-1111-111111111111"))
        self.assertFalse(valid_client_id("not-a-client-id"))


if __name__ == "__main__":
    unittest.main()
