"""Unit tests for the live auto-updater module."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from updater import (
    UpdateInfo,
    check_for_updates,
    generate_updater_script,
    is_newer_version,
    parse_version_tuple,
)


class UpdaterTests(unittest.TestCase):
    def test_parse_version_tuple(self) -> None:
        self.assertEqual(parse_version_tuple("1.0.0"), (1, 0, 0))
        self.assertEqual(parse_version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version_tuple("2.0"), (2, 0))
        self.assertEqual(parse_version_tuple("1.10.4-beta"), (1, 10, 4))
        self.assertEqual(parse_version_tuple("invalid"), (0,))

    def test_is_newer_version(self) -> None:
        self.assertTrue(is_newer_version("1.0.2", "1.0.1"))
        self.assertTrue(is_newer_version("1.1.0", "1.0.9"))
        self.assertTrue(is_newer_version("2.0.0", "1.99.99"))
        self.assertTrue(is_newer_version("v1.0.2", "1.0.1"))

        self.assertFalse(is_newer_version("1.0.1", "1.0.1"))
        self.assertFalse(is_newer_version("1.0.0", "1.0.1"))
        self.assertFalse(is_newer_version("0.9.9", "1.0.0"))

    @patch("urllib.request.urlopen")
    def test_check_for_updates_finds_newer(self, mock_urlopen) -> None:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"version": "1.0.2", "download_url": "https://example.com/Theara.zip", "changelog": "Bug fixes"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        update = check_for_updates("https://example.com/version.json", current_version="1.0.1")
        self.assertIsNotNone(update)
        self.assertEqual(update.version, "1.0.2")
        self.assertEqual(update.download_url, "https://example.com/Theara.zip")
        self.assertEqual(update.changelog, "Bug fixes")

    @patch("urllib.request.urlopen")
    def test_check_for_updates_returns_none_when_current(self, mock_urlopen) -> None:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"version": "1.0.1", "download_url": "https://example.com/Theara.zip"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        update = check_for_updates("https://example.com/version.json", current_version="1.0.1")
        self.assertIsNone(update)

    @patch("urllib.request.urlopen")
    def test_check_for_updates_handles_network_errors_safely(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = OSError("Network unreachable")

        update = check_for_updates("https://example.com/version.json", current_version="1.0.1")
        self.assertIsNone(update)

    def test_generate_updater_script(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "Theara.zip"
            app_dir = Path(td) / "App"
            zip_path.touch()
            app_dir.mkdir()

            script = generate_updater_script(zip_path, app_dir, "Theara.exe")
            self.assertTrue(script.is_file())
            content = script.read_text(encoding="utf-8")
            self.assertIn("Theara Live Updater", content)
            self.assertIn("Expand-Archive", content)
            self.assertIn("Theara.exe", content)


if __name__ == "__main__":
    unittest.main()
