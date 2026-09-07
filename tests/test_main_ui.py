import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QHeaderView

import main as app_module
from yandex_mail import Account, DeleteMailResult, FetchResult, MailMessage


class _FakeOAuth:
    def get_access_token(self, email):
        return "fake-test-token" if email.endswith("@outlook.com") else ""


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.originals = (
            app_module.ACCOUNT_STORE,
            app_module.SETTINGS_FILE,
            app_module.MICROSOFT_TOKEN_CACHE,
            app_module.fetch_latest_code,
            app_module.delete_all_yandex_mail,
        )
        root = Path(self.folder.name)
        app_module.ACCOUNT_STORE = root / "accounts.secure.json"
        app_module.SETTINGS_FILE = root / "settings.json"
        app_module.MICROSOFT_TOKEN_CACHE = root / "microsoft.secure.json"

    def tearDown(self):
        (
            app_module.ACCOUNT_STORE,
            app_module.SETTINGS_FILE,
            app_module.MICROSOFT_TOKEN_CACHE,
            app_module.fetch_latest_code,
            app_module.delete_all_yandex_mail,
        ) = self.originals
        self.folder.cleanup()

    def test_provider_column_and_secret_masking(self):
        window = app_module.MainWindow()
        try:
            window.accounts = [
                Account("demo@yandex.com", "fake-yandex", "auto"),
                Account("demo@company.example", "fake-zoho", "zoho", "imappro.zoho.eu"),
                Account("demo@outlook.com", "", "microsoft"),
            ]
            window._rebuild_table()
            self.assertEqual(window.table.item(0, window.COL_PROVIDER).text(), "Yandex Mail")
            self.assertEqual(window.table.item(1, window.COL_PROVIDER).text(), "Zoho Mail")
            self.assertEqual(window.table.item(2, window.COL_PROVIDER).text(), "Outlook / Hotmail")
            visible = " ".join(
                window.table.item(row, window.COL_PASSWORD).text()
                for row in range(window.table.rowCount())
            )
            self.assertNotIn("fake-yandex", visible)
            self.assertNotIn("fake-zoho", visible)
            self.assertIn("OAuth", visible)
        finally:
            window.close()

    def test_default_navigation_has_three_ordered_modes_and_yandex_mail_page(self):
        window = app_module.MainWindow()
        try:
            self.assertEqual(
                list(window.view_buttons),
                [window.VIEW_YANDEX, window.VIEW_MICROSOFT, window.VIEW_2FA],
            )
            self.assertEqual(
                [button.text() for button in window.view_buttons.values()],
                ["Yandex Mail", "Hotmail / Outlook", "Get 2FA Code"],
            )
            self.assertTrue(window.mode_button_group.exclusive())
            self.assertTrue(window.yandex_view_button.isChecked())
            self.assertFalse(window.microsoft_view_button.isChecked())
            self.assertFalse(window.twofa_view_button.isChecked())
            self.assertEqual(window._current_view, window.VIEW_YANDEX)
            self.assertEqual(window._mail_view, window.VIEW_YANDEX)
            self.assertEqual(window.page_stack.count(), 2)
            self.assertIs(window.page_stack.currentWidget(), window.mail_page)
            self.assertEqual(window.page_stack.indexOf(window.mail_page), 0)
            self.assertGreaterEqual(window.page_stack.indexOf(window.twofa_page), 0)
        finally:
            window.close()

    def test_provider_switch_filters_both_tables_without_reordering_or_removing_rows(self):
        window = app_module.MainWindow()
        try:
            yandex = Account("first@yandex.com", "fake", "yandex")
            microsoft = Account("second@outlook.com", "", "microsoft")
            window.accounts = [yandex, microsoft]
            window.results = {
                yandex.email.casefold(): FetchResult(
                    "found",
                    messages=(
                        MailMessage(
                            "111111",
                            "Yandex code",
                            "Yandex sender",
                            "Now",
                            "Yandex body",
                            recipient="first+team@yandex.com",
                            received_timestamp=20.0,
                        ),
                    ),
                ),
                microsoft.email.casefold(): FetchResult(
                    "found",
                    messages=(
                        MailMessage(
                            "222222",
                            "Microsoft code",
                            "Microsoft sender",
                            "Now",
                            "Microsoft body",
                            recipient=microsoft.email,
                            received_timestamp=10.0,
                        ),
                    ),
                ),
            }
            window._rebuild_table()

            def recent_visibility():
                return {
                    str(
                        window.recent_mail_table.item(
                            row, window.MAIL_COL_ACCOUNT
                        ).data(window.MAIL_ACCOUNT_KEY_ROLE)
                    ): window.recent_mail_table.isRowHidden(row)
                    for row in range(window.recent_mail_table.rowCount())
                }

            self.assertEqual(window.table.rowCount(), 2)
            self.assertEqual(window.recent_mail_table.rowCount(), 2)
            self.assertFalse(window.table.isRowHidden(0))
            self.assertTrue(window.table.isRowHidden(1))
            self.assertEqual(
                recent_visibility(),
                {yandex.email.casefold(): False, microsoft.email.casefold(): True},
            )

            window._switch_view(window.VIEW_MICROSOFT, announce=False)

            self.assertEqual(window.table.rowCount(), 2)
            self.assertEqual(window.recent_mail_table.rowCount(), 2)
            self.assertTrue(window.table.isRowHidden(0))
            self.assertFalse(window.table.isRowHidden(1))
            self.assertEqual(
                recent_visibility(),
                {yandex.email.casefold(): True, microsoft.email.casefold(): False},
            )
            self.assertEqual(
                [window.accounts[row].email for row in range(window.table.rowCount())],
                [yandex.email, microsoft.email],
            )

            window._switch_view(window.VIEW_YANDEX, announce=False)
            self.assertFalse(window.table.isRowHidden(0))
            self.assertTrue(window.table.isRowHidden(1))
        finally:
            window.close()

    def test_fetch_all_scopes_accounts_to_the_active_mail_provider(self):
        window = app_module.MainWindow()
        try:
            yandex = Account("fetch@yandex.com", "fake", "yandex")
            zoho = Account("fetch@zoho.com", "fake", "zoho")
            microsoft = Account("fetch@outlook.com", "", "microsoft")
            window.accounts = [yandex, zoho, microsoft]
            window._rebuild_table()

            with patch.object(window, "_start_fetch") as start_fetch:
                window._fetch_all()
                start_fetch.assert_called_once_with([yandex, zoho])

                start_fetch.reset_mock()
                window._switch_view(window.VIEW_MICROSOFT, announce=False)
                window._fetch_all()
                start_fetch.assert_called_once_with([microsoft])
        finally:
            window.close()

    def test_provider_switch_updates_controls_labels_and_all_mail_headers(self):
        window = app_module.MainWindow()
        try:
            self.assertFalse(window.alias_button.isHidden())
            self.assertFalse(window.delete_all_yandex_button.isHidden())
            self.assertFalse(window.paste_button.isHidden())
            self.assertFalse(window.import_button.isHidden())
            self.assertTrue(window.microsoft_button.isHidden())
            self.assertEqual(window.add_button.text(), "＋ Add")
            self.assertEqual(window.fetch_all_button.text(), "↻  Fetch Yandex")
            self.assertEqual(window.recent_mail_title.text(), "All Mail")
            self.assertEqual(
                window.recent_mail_table.horizontalHeaderItem(
                    window.MAIL_COL_SUBMAIL
                ).text(),
                "YANDEX SUBMAIL",
            )
            self.assertEqual(
                window.recent_mail_table.horizontalHeaderItem(
                    window.MAIL_COL_FACEBOOK_NAME
                ).text(),
                "FACEBOOK NAME",
            )

            window._switch_view(window.VIEW_MICROSOFT, announce=False)

            self.assertTrue(window.alias_button.isHidden())
            self.assertTrue(window.delete_all_yandex_button.isHidden())
            self.assertTrue(window.paste_button.isHidden())
            self.assertTrue(window.import_button.isHidden())
            self.assertFalse(window.microsoft_button.isHidden())
            self.assertEqual(window.add_button.text(), "＋ Add Microsoft")
            self.assertEqual(window.fetch_all_button.text(), "↻  Fetch Microsoft")
            self.assertEqual(
                window.recent_mail_title.text(), "All Mail — Hotmail / Outlook"
            )
            self.assertEqual(
                window.recent_mail_table.horizontalHeaderItem(
                    window.MAIL_COL_SUBMAIL
                ).text(),
                "RECIPIENT",
            )
            self.assertEqual(
                window.recent_mail_table.horizontalHeaderItem(
                    window.MAIL_COL_FACEBOOK_NAME
                ).text(),
                "SENDER / NAME",
            )
            self.assertEqual(
                window.recent_filter_edit.placeholderText(),
                "Sender name or recipient…",
            )
        finally:
            window.close()

    def test_switching_to_twofa_and_back_preserves_mail_data_and_filters(self):
        window = app_module.MainWindow()
        try:
            account = Account("preserve@outlook.com", "", "microsoft")
            result = FetchResult(
                "found",
                code="482910",
                messages=(
                    MailMessage(
                        "482910",
                        "Preserved subject",
                        "Preserved sender",
                        "Now",
                        "Preserved body",
                        recipient=account.email,
                    ),
                ),
            )
            window.accounts = [account]
            window.results[account.email.casefold()] = result
            window._rebuild_table()
            window._switch_view(window.VIEW_MICROSOFT, announce=False)
            window.filter_edit.setText("preserved")
            window.recent_filter_edit.setText("sender")
            account_rows = window.table.rowCount()
            recent_rows = window.recent_mail_table.rowCount()

            window._switch_view(window.VIEW_2FA, announce=False)
            self.assertIs(window.page_stack.currentWidget(), window.twofa_page)
            self.assertEqual(window._mail_view, window.VIEW_MICROSOFT)

            window._switch_view(window.VIEW_MICROSOFT, announce=False)

            self.assertIs(window.page_stack.currentWidget(), window.mail_page)
            self.assertEqual(window._current_view, window.VIEW_MICROSOFT)
            self.assertEqual(window.filter_edit.text(), "preserved")
            self.assertEqual(window.recent_filter_edit.text(), "sender")
            self.assertIs(window.results[account.email.casefold()], result)
            self.assertEqual(window.table.rowCount(), account_rows)
            self.assertEqual(window.recent_mail_table.rowCount(), recent_rows)
            self.assertEqual(
                window.recent_mail_table.item(0, window.MAIL_COL_CODE).text(),
                result.code,
            )
        finally:
            window.close()

    def test_fetch_controls_disable_and_restore_all_navigation_buttons(self):
        window = app_module.MainWindow()
        try:
            window._set_fetch_controls(True)
            self.assertTrue(
                all(not button.isEnabled() for button in window.view_buttons.values())
            )
            self.assertTrue(window.stop_button.isEnabled())

            window._set_fetch_controls(False)
            self.assertTrue(
                all(button.isEnabled() for button in window.view_buttons.values())
            )
            self.assertFalse(window.stop_button.isEnabled())
            self.assertTrue(window.yandex_view_button.isChecked())
        finally:
            window.close()

    def test_twofa_generate_copy_and_clear_uses_local_helpers(self):
        window = app_module.MainWindow()
        try:
            window._switch_view(window.VIEW_2FA, announce=False)
            self.assertTrue(window.totp_show_secret.isChecked())
            self.assertEqual(
                window.totp_secret_edit.echoMode(),
                window.totp_secret_edit.EchoMode.Normal,
            )
            window.totp_secret_edit.setText("JBSWY3DPEHPK3PXP")
            with (
                patch.object(app_module, "generate_totp", return_value="123456") as generate,
                patch.object(app_module, "totp_seconds_remaining", return_value=17),
            ):
                window.totp_generate_button.click()

            self.assertGreaterEqual(generate.call_count, 2)
            self.assertEqual(window.totp_code_label.text(), "123456")
            self.assertEqual(window.totp_countdown_label.text(), "New code in 17 second(s)")
            self.assertEqual(window.totp_progress.value(), 17)
            self.assertFalse(window.totp_error_label.isVisible())
            self.assertTrue(window.totp_copy_button.isEnabled())

            window.totp_copy_button.click()
            self.assertEqual(self.app.clipboard().text(), "123456")

            window.totp_show_secret.setChecked(False)
            self.assertEqual(
                window.totp_secret_edit.echoMode(),
                window.totp_secret_edit.EchoMode.Password,
            )
            window.totp_clear_button.click()
            self.assertEqual(window.totp_secret_edit.text(), "")
            self.assertEqual(window.totp_secret_edit.echoMode(), window.totp_secret_edit.EchoMode.Normal)
            self.assertEqual(window.totp_code_label.text(), "——————")
            self.assertEqual(window.totp_progress.value(), 0)
            self.assertFalse(window.totp_copy_button.isEnabled())
            self.assertTrue(window.totp_show_secret.isChecked())
        finally:
            window.totp_timer.stop()
            window.close()

    def test_twofa_invalid_secret_shows_sanitized_error_state(self):
        window = app_module.MainWindow()
        try:
            window._switch_view(window.VIEW_2FA, announce=False)
            invalid_secret = "not a valid private secret"
            window.totp_secret_edit.setText(invalid_secret)
            with patch.object(
                app_module,
                "generate_totp",
                side_effect=app_module.InvalidTOTPSecret("TOTP secret is invalid."),
            ):
                window.totp_generate_button.click()

            self.assertEqual(window.totp_code_label.text(), "——————")
            self.assertEqual(window.totp_progress.value(), 0)
            self.assertFalse(window.totp_copy_button.isEnabled())
            self.assertFalse(window.totp_error_label.isHidden())
            self.assertIn("valid base32", window.totp_countdown_label.text().casefold())
            self.assertNotIn(invalid_secret, window.totp_error_label.text())
            self.assertEqual(window._totp_active_secret, "")
        finally:
            window.totp_timer.stop()
            window.close()

    def test_ctrl_f_routes_to_twofa_secret_field(self):
        window = app_module.MainWindow()
        try:
            window.show()
            window._switch_view(window.VIEW_2FA, announce=False)
            window.totp_secret_edit.setText("JBSWY3DPEHPK3PXP")
            window.twofa_view_button.setFocus()
            self.app.processEvents()

            QTest.keyClick(
                window.twofa_view_button,
                Qt.Key.Key_F,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.app.processEvents()

            self.assertIs(self.app.focusWidget(), window.totp_secret_edit)
            self.assertEqual(
                window.totp_secret_edit.selectedText(), "JBSWY3DPEHPK3PXP"
            )
        finally:
            window.totp_timer.stop()
            window.close()

    def test_twofa_long_screen_multiline_generates_table_and_copies_all(self):
        window = app_module.MainWindow()
        try:
            window._switch_view(window.VIEW_2FA, announce=False)
            input_text = "AccountA|JBSWY3DPEHPK3PXP\nAccountB|JBSWY3DPEHPK3PXQ"
            window.twofa_text_edit.setPlainText(input_text)
            self.assertEqual(window.twofa_keys_badge.text(), "Accounts: 2/1000")

            with (
                patch.object(app_module, "generate_totp", side_effect=["111111", "111111", "222222", "111111", "111111", "222222"]),
                patch.object(app_module, "totp_seconds_remaining", return_value=25),
            ):
                window.totp_generate_button.click()

            self.assertEqual(window.twofa_results_table.rowCount(), 2)
            self.assertEqual(window.twofa_results_table.item(0, 1).text(), "AccountA")
            self.assertEqual(window.twofa_results_table.item(1, 1).text(), "AccountB")
            self.assertEqual(window.twofa_results_table.item(0, 3).text(), "111111")
            self.assertEqual(window.twofa_results_table.item(1, 3).text(), "222222")
            self.assertIn("Total: 2 | Codes: 2", window.twofa_results_stats.text())

            # Test Copy All
            window._copy_all_twofa_codes()
            self.assertEqual(self.app.clipboard().text(), "111111\n222222")

            # Test toggling secret visibility in table
            window.totp_show_secret.setChecked(False)
            self.assertTrue(window.twofa_results_table.item(0, 2).text().startswith("•"))
            window.totp_show_secret.setChecked(True)
            self.assertEqual(window.twofa_results_table.item(0, 2).text(), "JBSWY3DPEHPK3PXP")
        finally:
            window.totp_timer.stop()
            window.close()

    def test_twofa_table_filter_and_row_selection(self):
        window = app_module.MainWindow()
        try:
            window._switch_view(window.VIEW_2FA, announce=False)
            window.twofa_text_edit.setPlainText("Alpha|JBSWY3DPEHPK3PXP\nBeta|JBSWY3DPEHPK3PXQ")
            with patch.object(app_module, "generate_totp", return_value="654321"):
                window.totp_generate_button.click()

            self.assertEqual(window.twofa_results_table.rowCount(), 2)

            # Test filter
            window._filter_twofa_results("Beta")
            self.assertTrue(window.twofa_results_table.isRowHidden(0))
            self.assertFalse(window.twofa_results_table.isRowHidden(1))

            window._filter_twofa_results("")
            self.assertFalse(window.twofa_results_table.isRowHidden(0))
            self.assertFalse(window.twofa_results_table.isRowHidden(1))

            # Test clicking row 1 updates active code in banner
            with patch.object(app_module, "generate_totp", return_value="999888"):
                window._on_twofa_table_cell_clicked(1, 0)
            self.assertEqual(window.totp_code_label.text(), "999888")
        finally:
            window.totp_timer.stop()
            window.close()

    def test_twofa_live_updates_on_typing_and_clearing(self):
        window = app_module.MainWindow()
        try:
            window._switch_view(window.VIEW_2FA, announce=False)
            self.assertFalse(window.twofa_empty_label.isHidden())
            self.assertTrue(window.twofa_active_container.isHidden())

            # Typing/pasting a valid key live-generates code immediately without clicking Start
            with patch.object(app_module, "generate_totp", return_value="777888"):
                window.twofa_text_edit.setPlainText("JBSWY3DPEHPK3PXP")

            self.assertTrue(window.twofa_empty_label.isHidden())
            self.assertFalse(window.twofa_active_container.isHidden())
            self.assertEqual(window.totp_code_label.text(), "777888")
            self.assertTrue(window.totp_copy_button.isEnabled())

            # Clearing input resets smoothly to empty state
            window.twofa_text_edit.clear()
            self.assertFalse(window.twofa_empty_label.isHidden())
            self.assertTrue(window.twofa_active_container.isHidden())
            self.assertEqual(window.totp_code_label.text(), "——————")
        finally:
            window.totp_timer.stop()
            window.close()

    def test_footer_credit_and_copyright_labels_are_exact(self):
        window = app_module.MainWindow()
        try:
            self.assertEqual(window.created_by_label.text(), "Created by Theara")
            self.assertEqual(
                window.copyright_label.text(),
                "Copyright © 2026 · All Rights Reserved",
            )
        finally:
            window.close()

    def test_legacy_data_migration_copies_once_without_overwriting(self):
        root = Path(self.folder.name)
        legacy = root / "legacy"
        destination = root / "local-app-data"
        legacy.mkdir()
        destination.mkdir()
        (legacy / "accounts.secure.json").write_bytes(b"encrypted-account-test")
        (legacy / "settings.json").write_text('{"legacy":true}', encoding="utf-8")
        (destination / "settings.json").write_text('{"new":true}', encoding="utf-8")

        with (
            patch.object(app_module, "LEGACY_DATA_DIR", legacy),
            patch.object(app_module, "APP_DIR", destination),
            patch.object(app_module, "ACCOUNT_STORE", destination / "accounts.secure.json"),
            patch.object(app_module, "SETTINGS_FILE", destination / "settings.json"),
            patch.object(
                app_module,
                "MICROSOFT_TOKEN_CACHE",
                destination / "microsoft_token_cache.secure.json",
            ),
        ):
            self.assertEqual(app_module.migrate_legacy_app_data(), 1)
            self.assertEqual(app_module.migrate_legacy_app_data(), 0)

        self.assertEqual(
            (destination / "accounts.secure.json").read_bytes(),
            b"encrypted-account-test",
        )
        self.assertEqual(
            (destination / "settings.json").read_text(encoding="utf-8"),
            '{"new":true}',
        )
        self.assertTrue((legacy / "accounts.secure.json").is_file())

    def test_yandex_alias_is_kept_without_a_visible_table_column(self):
        window = app_module.MainWindow()
        try:
            alias = "demo+432teamC@yandex.com"
            window.accounts = [
                Account("demo@yandex.com", "fake-yandex", "auto", mail_alias=alias),
                Account("demo@zoho.com", "fake-zoho", "zoho"),
            ]
            window._rebuild_table()

            headers = [
                window.table.horizontalHeaderItem(column).text()
                for column in range(window.table.columnCount())
            ]
            self.assertNotIn("YANDEX ALIAS", headers)
            self.assertEqual(window.accounts[0].mail_alias, alias)
        finally:
            window.close()

    def test_yandex_alias_validation_is_mailbox_and_provider_aware(self):
        yandex = Account("Demo@yandex.com", "fake", "auto")
        valid_aliases = (
            "Demo+100teamA@yandex.com",
            "demo+432teamC@YANDEX.COM",
            "",
        )
        invalid_aliases = (
            "demo@yandex.com",
            "demo+@yandex.com",
            "other+100teamA@yandex.com",
            "demo+100teamA@ya.ru",
            "demo+team A@yandex.com",
            "demo+teamA@yandex.com|ignored",
        )

        for alias in valid_aliases:
            with self.subTest(alias=alias):
                valid, detail = app_module.validate_mail_alias(yandex, alias)
                self.assertTrue(valid, detail)
        for alias in invalid_aliases:
            with self.subTest(alias=alias):
                valid, detail = app_module.validate_mail_alias(yandex, alias)
                self.assertFalse(valid)
                self.assertTrue(detail)

        valid, detail = app_module.validate_mail_alias(
            Account("demo@zoho.com", "fake", "zoho"),
            "demo+teamA@zoho.com",
        )
        self.assertFalse(valid)
        self.assertIn("Yandex", detail)

    def test_password_update_preserves_existing_mail_alias(self):
        window = app_module.MainWindow()
        try:
            alias = "demo+128teamD@yandex.com"
            window.accounts = [
                Account("demo@yandex.com", "old-password", "auto", mail_alias=alias)
            ]
            window._rebuild_table()

            with patch.object(window, "_save_accounts", return_value=True) as save_accounts:
                added, updated = window._add_accounts(
                    [Account("DEMO@yandex.com", "new-password", "auto")]
                )

            self.assertEqual((added, updated), (0, 1))
            self.assertEqual(window.accounts[0].password, "new-password")
            self.assertEqual(window.accounts[0].mail_alias, alias)
            save_accounts.assert_called_once()
        finally:
            window.close()

    def test_editing_and_clearing_alias_updates_encrypted_profile_view(self):
        window = app_module.MainWindow()
        try:
            account = Account("demo@yandex.com", "fake", "auto")
            key = account.email.casefold()
            alias = "demo+90teamE@yandex.com"
            window.accounts = [account]
            window.results[key] = FetchResult("found", code="482910", body="Old result")
            window._rebuild_table()

            with (
                patch.object(
                    app_module.QInputDialog,
                    "getText",
                    side_effect=[(alias, True), ("", True)],
                ),
                patch.object(window, "_save_accounts", return_value=True) as save_accounts,
            ):
                window._edit_mail_alias(0)
                self.assertEqual(window.accounts[0].mail_alias, alias)
                self.assertNotIn(key, window.results)

                window._edit_mail_alias(0)
                self.assertEqual(window.accounts[0].mail_alias, "")

            self.assertEqual(save_accounts.call_count, 2)
        finally:
            window.close()

    def test_background_fetch_passes_oauth_provider_and_updates_result(self):
        calls = []

        def fake_fetch(account, **kwargs):
            token_provider = kwargs.get("access_token_provider")
            calls.append(
                (
                    account.provider,
                    bool(token_provider and token_provider(account)),
                    kwargs.get("max_messages"),
                    kwargs.get("max_age_hours"),
                )
            )
            return FetchResult("found", code="482910", subject="Verification code")

        app_module.fetch_latest_code = fake_fetch
        window = app_module.MainWindow()
        try:
            window.microsoft_oauth = _FakeOAuth()
            window.accounts = [Account("demo@outlook.com", "", "microsoft")]
            window._rebuild_table()
            window._switch_view(window.VIEW_MICROSOFT, announce=False)
            window._fetch_all()
            deadline = time.time() + 5
            while window._active and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()
            self.assertFalse(window._active)
            self.assertEqual(calls, [("microsoft", True, 0, 1)])
            window.table.selectRow(0)
            self.assertEqual(window._selected_code(), "482910")
        finally:
            window.close()

    def test_delete_all_yandex_mail_cancel_keeps_mail(self):
        window = app_module.MainWindow()
        try:
            account = Account("demo@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window._rebuild_table()
            window.table.selectRow(0)

            with (
                patch.object(
                    app_module.QMessageBox,
                    "warning",
                    return_value=app_module.QMessageBox.StandardButton.Cancel,
                ) as confirmation,
                patch.object(window.thread_pool, "start") as start,
            ):
                window._delete_all_yandex_mail_for_selection()

            prompt = confirmation.call_args.args[2]
            self.assertIn(account.email, prompt)
            self.assertIn("PERMANENTLY DELETE ALL MAIL", prompt)
            self.assertIn("Inbox", prompt)
            self.assertIn("Subscriptions", prompt)
            self.assertIn("Social", prompt)
            self.assertIn("Sent", prompt)
            self.assertIn("Drafts", prompt)
            self.assertIn("Outbox", prompt)
            self.assertIn("Archive", prompt)
            self.assertIn("Spam", prompt)
            self.assertIn("Trash", prompt)
            self.assertIn("custom folders", prompt)
            self.assertIn("cannot be restored", prompt)
            self.assertIn("alias preference is ignored", prompt)
            self.assertIn("Click OK", prompt)
            self.assertEqual(
                confirmation.call_args.args[3],
                app_module.QMessageBox.StandardButton.Ok
                | app_module.QMessageBox.StandardButton.Cancel,
            )
            self.assertEqual(
                confirmation.call_args.args[4],
                app_module.QMessageBox.StandardButton.Cancel,
            )
            start.assert_not_called()
            self.assertFalse(window._active)
        finally:
            window.close()

    def test_delete_all_yandex_mail_uses_multiple_selected_accounts(self):
        window = app_module.MainWindow()
        try:
            first = Account("first@yandex.com", "fake", "yandex")
            second = Account("second@yandex.com", "fake", "yandex")
            window.accounts = [first, second, Account("other@zoho.com", "fake", "zoho")]
            window._rebuild_table()
            flags = (
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows
            )
            selection = window.table.selectionModel()
            selection.select(window.table.model().index(0, 0), flags)
            selection.select(window.table.model().index(1, 0), flags)

            with (
                patch.object(
                    app_module.QMessageBox,
                    "warning",
                    return_value=app_module.QMessageBox.StandardButton.Ok,
                ),
                patch.object(window, "_start_delete_all_yandex_mail") as start,
            ):
                window._delete_all_yandex_mail_for_selection()

            selected_accounts = start.call_args.args[0]
            self.assertEqual(
                [account.email for account in selected_accounts],
                [first.email, second.email],
            )
        finally:
            window.close()

    def test_delete_all_yandex_mail_shows_live_multi_account_progress(self):
        window = app_module.MainWindow()
        try:
            first = Account("first@yandex.com", "fake", "yandex")
            second = Account("second@yandex.com", "fake", "yandex")
            window.accounts = [first, second]
            window._rebuild_table()

            with patch.object(window.thread_pool, "start"):
                window._start_delete_all_yandex_mail([first, second])

            self.assertEqual(window.checked_card.value.text(), "0 / 2")
            self.assertEqual(window.delete_all_yandex_button.text(), "Deleting 0 / 2...")
            self.assertEqual(
                window.table.item(0, window.COL_STATUS).text(),
                "Waiting to delete...",
            )
            self.assertEqual(
                window.table.item(1, window.COL_STATUS).text(),
                "Waiting to delete...",
            )

            window._delete_all_yandex_mail_started(window._run_id, first.email)
            self.assertEqual(window.delete_all_yandex_button.text(), "Deleting 1 / 2...")
            self.assertEqual(
                window.table.item(0, window.COL_STATUS).text(),
                "Deleting all mail...",
            )
            self.assertIn(first.email, window.statusBar().currentMessage())

            window._delete_all_yandex_mail_completed(
                window._run_id,
                first.email,
                DeleteMailResult("deleted", deleted_count=2, folder_count=1),
            )
            self.assertEqual(window.checked_card.value.text(), "1 / 2")
            self.assertIn("Completed 1 / 2", window.statusBar().currentMessage())

            window._delete_all_yandex_mail_started(window._run_id, second.email)
            self.assertEqual(window.delete_all_yandex_button.text(), "Deleting 2 / 2...")
            window._delete_all_yandex_mail_completed(
                window._run_id,
                second.email,
                DeleteMailResult("empty"),
            )
            self.assertFalse(window._active)
            self.assertEqual(
                window.delete_all_yandex_button.text(),
                "Delete selected Yandex mail",
            )
            self.assertIn("Permanently deleted 2", window.statusBar().currentMessage())
        finally:
            window.close()

    def test_delete_all_yandex_mail_runs_in_background_and_clears_stale_mail(self):
        calls = []

        def fake_delete(account, **kwargs):
            calls.append((account.email, kwargs.get("timeout")))
            return DeleteMailResult(
                "deleted",
                deleted_count=3,
                folder_count=2,
                detail="Permanently deleted 3 message(s) from 2 Yandex folders.",
            )

        app_module.delete_all_yandex_mail = fake_delete
        window = app_module.MainWindow()
        try:
            yandex = Account(
                "demo@yandex.com",
                "fake",
                "yandex",
                mail_alias="demo+team@yandex.com",
            )
            zoho = Account("demo@zoho.com", "fake", "zoho")
            key = yandex.email.casefold()
            window.accounts = [yandex, zoho]
            window.results[key] = FetchResult(
                "found",
                code="123456",
                messages=(MailMessage("123456", "Old", "Sender", "Now", "Body"),),
            )
            window._rebuild_table()
            window.table.selectRow(0)

            with patch.object(
                app_module.QMessageBox,
                "warning",
                return_value=app_module.QMessageBox.StandardButton.Ok,
            ):
                window._delete_all_yandex_mail_for_selection()

            deadline = time.time() + 5
            while window._active and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()

            self.assertFalse(window._active)
            self.assertEqual(calls, [(yandex.email, 18)])
            self.assertNotIn(key, window.results)
            self.assertEqual(window.recent_mail_table.rowCount(), 0)
            self.assertEqual(
                window.table.item(0, window.COL_SUBJECT).text(),
                "All mailbox folders emptied",
            )
            self.assertEqual(window.table.item(0, window.COL_STATUS).text(), "3 deleted")
            self.assertTrue(window.delete_all_yandex_button.isEnabled())
            self.assertFalse(window.stop_button.isEnabled())
            self.assertIn("Permanently deleted 3 Yandex message(s)", window.statusBar().currentMessage())
            self.assertIn("from 2 folder(s)", window.statusBar().currentMessage())
        finally:
            window.close()

    def test_delete_all_yandex_mail_never_uses_default_checked_rows(self):
        window = app_module.MainWindow()
        try:
            window.accounts = [Account("demo@yandex.com", "fake", "yandex")]
            window._rebuild_table()
            self.assertEqual(
                window.table.item(0, window.COL_CHECK).checkState(),
                app_module.Qt.CheckState.Checked,
            )

            with patch.object(app_module.QMessageBox, "warning") as confirmation:
                window._delete_all_yandex_mail_for_selection()

            confirmation.assert_not_called()
            self.assertIn("Select", window.statusBar().currentMessage())
            self.assertFalse(window._active)
        finally:
            window.close()

    def test_hidden_selected_yandex_row_is_excluded(self):
        window = app_module.MainWindow()
        try:
            window.accounts = [Account("hidden@yandex.com", "fake", "yandex")]
            window._rebuild_table()
            window.table.selectRow(0)
            window.table.setRowHidden(0, True)

            with patch.object(app_module.QMessageBox, "warning") as confirmation:
                window._delete_all_yandex_mail_for_selection()

            confirmation.assert_not_called()
            self.assertFalse(window._active)
        finally:
            window.close()

    def test_unknown_all_mail_delete_outcome_clears_stale_mail_and_requires_inspection(self):
        def fake_delete(_account, **_kwargs):
            return DeleteMailResult(
                "outcome_unknown",
                detail=(
                    "Yandex could not confirm whether all messages were permanently deleted. "
                    "Check every folder before trying again."
                ),
            )

        app_module.delete_all_yandex_mail = fake_delete
        window = app_module.MainWindow()
        try:
            account = Account("unknown@yandex.com", "fake", "yandex")
            key = account.email.casefold()
            window.accounts = [account]
            window.results[key] = FetchResult(
                "found",
                code="654321",
                messages=(MailMessage("654321", "Old", "Sender", "Now", "Body"),),
            )
            window._rebuild_table()
            window.table.selectRow(0)

            with patch.object(
                app_module.QMessageBox,
                "warning",
                return_value=app_module.QMessageBox.StandardButton.Ok,
            ):
                window._delete_all_yandex_mail_for_selection()

            deadline = time.time() + 5
            while window._active and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()

            self.assertNotIn(key, window.results)
            self.assertEqual(window.recent_mail_table.rowCount(), 0)
            self.assertEqual(
                window.table.item(0, window.COL_SUBJECT).text(),
                "Check every Yandex folder",
            )
            self.assertEqual(
                window.table.item(0, window.COL_STATUS).text(),
                "Check mailbox",
            )
            self.assertIn("could not be confirmed", window.statusBar().currentMessage())
        finally:
            window.close()

    def test_window_cannot_close_during_permanent_all_folder_delete(self):
        class CloseEvent:
            def __init__(self):
                self.ignored = False
                self.accepted = False

            def ignore(self):
                self.ignored = True

            def accept(self):
                self.accepted = True

        window = app_module.MainWindow()
        try:
            window._active = True
            window._active_action = "delete_all_mail"
            event = CloseEvent()
            with patch.object(app_module.QMessageBox, "information") as information:
                window.closeEvent(event)

            self.assertTrue(event.ignored)
            self.assertFalse(event.accepted)
            self.assertFalse(window._cancel_event.is_set())
            information.assert_called_once()
        finally:
            window._active = False
            window._active_action = ""
            window.close()

    def test_all_mail_populates_and_account_selection_does_not_hide_rows(self):
        window = app_module.MainWindow()
        try:
            first = Account("first@yandex.com", "fake", "yandex")
            second = Account("second@zoho.com", "fake", "zoho")
            first_messages = (
                MailMessage(
                    "111111",
                    "First subject",
                    "Alpha sender",
                    "Today 10:00",
                    "First body",
                    recipient="first+dynamic@yandex.com",
                    facebook_name="Alpha Person",
                ),
                MailMessage("222222", "Second subject", "Beta sender", "Today 09:00", "Second body"),
            )
            second_message = MailMessage(
                "333333", "Third subject", "Gamma sender", "Today 08:00", "Third body"
            )
            window.accounts = [first, second]
            window.results = {
                first.email.casefold(): FetchResult("found", messages=first_messages),
                second.email.casefold(): FetchResult("found", messages=(second_message,)),
            }
            window._rebuild_table()

            self.assertEqual(window.recent_mail_table.rowCount(), 3)
            self.assertEqual(
                window.recent_mail_table.item(0, window.MAIL_COL_ACCOUNT).text(),
                "first+dynamic@yandex.com",
            )
            self.assertEqual(
                window.recent_mail_table.item(0, window.MAIL_COL_FACEBOOK_NAME).text(),
                "Alpha Person",
            )
            self.assertEqual(
                window.recent_mail_table.item(2, window.MAIL_COL_CODE).text(),
                "333333",
            )

            window.table.selectRow(0)
            self.assertEqual(window.recent_mail_table.rowCount(), 3)

            window.table.selectRow(1)
            self.assertEqual(window.recent_mail_table.rowCount(), 3)

            window.table.clearSelection()
            window.table.setCurrentItem(None)
            window._account_selection_changed()
            self.assertEqual(window.recent_mail_table.rowCount(), 3)
        finally:
            window.close()

    def test_all_mail_puts_codes_first_and_toggle_restores_newest_first(self):
        window = app_module.MainWindow()
        try:
            account = Account("sort@yandex.com", "fake", "yandex")
            messages = (
                MailMessage(
                    "",
                    "Newest regular mail",
                    "Sender",
                    "10:30",
                    "No code",
                    received_timestamp=30.0,
                ),
                MailMessage(
                    "222222",
                    "Newest code mail",
                    "Sender",
                    "10:20",
                    "Code two",
                    received_timestamp=20.0,
                ),
                MailMessage(
                    "111111",
                    "Older code mail",
                    "Sender",
                    "10:10",
                    "Code one",
                    received_timestamp=10.0,
                ),
                MailMessage(
                    "",
                    "Oldest regular mail",
                    "Sender",
                    "10:00",
                    "No code",
                    received_timestamp=1.0,
                ),
            )
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found", messages=messages
            )
            window._rebuild_table()

            self.assertTrue(window.codes_first_checkbox.isChecked())
            self.assertEqual(
                [
                    window.recent_mail_table.item(
                        row, window.MAIL_COL_SUBJECT
                    ).text()
                    for row in range(4)
                ],
                [
                    "Newest code mail",
                    "Older code mail",
                    "Newest regular mail",
                    "Oldest regular mail",
                ],
            )
            self.assertIs(window._recent_message_for_row(0)[1], messages[1])

            window.codes_first_checkbox.setChecked(False)

            self.assertEqual(
                [
                    window.recent_mail_table.item(
                        row, window.MAIL_COL_SUBJECT
                    ).text()
                    for row in range(4)
                ],
                [
                    "Newest regular mail",
                    "Newest code mail",
                    "Older code mail",
                    "Oldest regular mail",
                ],
            )
            self.assertIs(window._recent_message_for_row(0)[1], messages[0])
        finally:
            window.close()

    def test_khmer_name_and_body_keep_unicode_and_use_font_fallback(self):
        khmer_name = "\u179f\u17bb\u1781 \u179b\u17b8\u1798"
        body_text = f"Hello {khmer_name}\nYour code is 123456"
        window = app_module.MainWindow()
        try:
            account = Account("khmer@yandex.com", "fake", "yandex")
            message = MailMessage(
                "123456",
                "Facebook confirmation",
                "Facebook <security@facebookmail.com>",
                "Now",
                body_text,
                facebook_name=khmer_name,
            )
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found", messages=(message,)
            )
            window._rebuild_table()

            name_item = window.recent_mail_table.item(
                0, window.MAIL_COL_FACEBOOK_NAME
            )
            self.assertEqual(name_item.text(), khmer_name)
            self.assertIn("Noto Sans Khmer", name_item.font().families())

            dialog = app_module.MessageBodyDialog(account, message, "Yandex Mail")
            try:
                self.assertEqual(dialog.body.toPlainText(), body_text)
                self.assertIn("Noto Sans Khmer", dialog.body.font().families())
            finally:
                dialog.close()
        finally:
            window.close()

    def test_text_filter_applies_to_recent_mail_and_account_tables(self):
        window = app_module.MainWindow()
        try:
            account = Account("filter@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found",
                code="111111",
                messages=(
                    MailMessage("111111", "Welcome", "Alpha sender", "Today", "One"),
                    MailMessage("222222", "Security notice", "Beta sender", "Yesterday", "Two"),
                ),
            )
            window._rebuild_table()

            window._filter_rows("security notice")
            self.assertTrue(window.table.isRowHidden(0))
            self.assertTrue(window.recent_mail_table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(1))

            window._filter_rows("filter@yandex.com")
            self.assertFalse(window.table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(1))

            window._filter_rows("111111")
            self.assertFalse(window.table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(0))
            self.assertTrue(window.recent_mail_table.isRowHidden(1))
        finally:
            window.close()

    def test_recent_mail_find_matches_facebook_name_and_dynamic_yandex_submail(self):
        window = app_module.MainWindow()
        try:
            account = Account("finder@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found",
                messages=(
                    MailMessage(
                        "111111",
                        "First notice",
                        "Facebook <security@facebookmail.com>",
                        "Today",
                        "One",
                        recipient="finder+TeamOne@yandex.com",
                        facebook_name="Dara Sokha",
                        received_timestamp=20.0,
                    ),
                    MailMessage(
                        "222222",
                        "Second notice",
                        "Facebook <security@facebookmail.com>",
                        "Yesterday",
                        "Two",
                        recipient="finder+TeamTwo@yandex.com",
                        facebook_name="Srey Mom",
                        received_timestamp=10.0,
                    ),
                ),
            )
            window._rebuild_table()

            window.recent_filter_edit.setText("ARA SOK")
            self.assertFalse(window.table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(0))
            self.assertTrue(window.recent_mail_table.isRowHidden(1))
            self.assertEqual(window.recent_match_label.text(), "1 of 2")

            window.recent_filter_edit.setText("teamTWO@YANDEX")
            self.assertFalse(window.table.isRowHidden(0))
            self.assertTrue(window.recent_mail_table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(1))
            self.assertEqual(window.recent_match_label.text(), "1 of 2")

            window.recent_filter_edit.clear()
            self.assertFalse(window.table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(0))
            self.assertFalse(window.recent_mail_table.isRowHidden(1))
            self.assertEqual(window.recent_match_label.text(), "2 of 2")
        finally:
            window.close()

    def test_recent_mail_find_combines_with_global_filter_and_updates_empty_state(self):
        window = app_module.MainWindow()
        try:
            account = Account("combined@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found",
                messages=(
                    MailMessage(
                        "111111",
                        "Security alert",
                        "Sender One",
                        "Today",
                        "One",
                        recipient="combined+one@yandex.com",
                        facebook_name="Dara Sokha",
                        received_timestamp=30.0,
                    ),
                    MailMessage(
                        "222222",
                        "Security alert",
                        "Sender Two",
                        "Yesterday",
                        "Two",
                        recipient="combined+two@yandex.com",
                        facebook_name="Srey Mom",
                        received_timestamp=20.0,
                    ),
                    MailMessage(
                        "333333",
                        "Welcome",
                        "Sender Three",
                        "Monday",
                        "Three",
                        recipient="combined+three@yandex.com",
                        facebook_name="Dara Sokha",
                        received_timestamp=10.0,
                    ),
                ),
            )
            window._rebuild_table()

            window.filter_edit.setText("security")
            window.recent_filter_edit.setText("dara")
            self.assertEqual(
                [window.recent_mail_table.isRowHidden(row) for row in range(3)],
                [False, True, True],
            )
            self.assertEqual(window.recent_match_label.text(), "1 of 3")
            self.assertTrue(window.recent_empty_label.isHidden())

            window.recent_filter_edit.setText("missing person")
            self.assertTrue(all(window.recent_mail_table.isRowHidden(row) for row in range(3)))
            self.assertEqual(window.recent_match_label.text(), "0 of 3")
            self.assertFalse(window.recent_empty_label.isHidden())
            self.assertEqual(
                window.recent_empty_label.text(),
                "No mail matches the current search.",
            )

            window.recent_filter_edit.clear()
            self.assertEqual(
                [window.recent_mail_table.isRowHidden(row) for row in range(3)],
                [False, False, True],
            )
            self.assertEqual(window.recent_match_label.text(), "2 of 3")
            self.assertTrue(window.recent_empty_label.isHidden())

            window.filter_edit.clear()
            self.assertFalse(any(window.recent_mail_table.isRowHidden(row) for row in range(3)))
            self.assertEqual(window.recent_match_label.text(), "3 of 3")
        finally:
            window.close()

    def test_ctrl_f_focuses_and_selects_recent_mail_find_text(self):
        window = app_module.MainWindow()
        try:
            window.show()
            self.app.processEvents()
            window.recent_filter_edit.setText("Dara Sokha")
            window.filter_edit.setFocus()
            self.app.processEvents()

            QTest.keyClick(
                window.filter_edit,
                Qt.Key.Key_F,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.app.processEvents()

            self.assertIs(self.app.focusWidget(), window.recent_filter_edit)
            self.assertEqual(window.recent_filter_edit.selectedText(), "Dara Sokha")
        finally:
            window.close()

    def test_recent_mail_row_metadata_is_key_and_index_only(self):
        window = app_module.MainWindow()
        try:
            account = Account("metadata@yandex.com", "never-store-this", "yandex")
            message = MailMessage("123456", "Subject", "Sender", "Now", "private body")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found", messages=(message,)
            )
            window._rebuild_table()

            item = window.recent_mail_table.item(0, window.MAIL_COL_ACCOUNT)
            self.assertEqual(
                item.data(window.MAIL_ACCOUNT_KEY_ROLE),
                account.email.casefold(),
            )
            self.assertEqual(item.data(window.MAIL_MESSAGE_INDEX_ROLE), 0)
            self.assertIsNot(item.data(window.MAIL_ACCOUNT_KEY_ROLE), message)
            visible_text = " ".join(
                window.recent_mail_table.item(0, column).text()
                for column in range(window.recent_mail_table.columnCount())
            )
            self.assertNotIn(account.password, visible_text)
            self.assertNotIn(message.body, visible_text)
        finally:
            window.close()

    def test_recent_mail_double_click_opens_exact_message_body(self):
        window = app_module.MainWindow()
        try:
            account = Account("body@yandex.com", "fake", "yandex")
            first = MailMessage("111111", "First", "Sender", "Now", "First exact body")
            second = MailMessage("222222", "Second", "Sender", "Later", "Second exact body")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found", messages=(first, second)
            )
            window._rebuild_table()

            with patch.object(app_module, "MessageBodyDialog") as dialog_class:
                window._recent_mail_double_clicked(1, window.MAIL_COL_SUBJECT)

            dialog_class.assert_called_once()
            self.assertIs(dialog_class.call_args.args[0], account)
            self.assertIs(dialog_class.call_args.args[1], second)
            dialog_class.return_value.exec.assert_called_once()
        finally:
            window.close()

    def test_recent_mail_empty_state_and_stale_fetch_cleanup(self):
        window = app_module.MainWindow()
        try:
            account = Account("empty@yandex.com", "fake", "yandex")
            key = account.email.casefold()
            window.accounts = [account]
            window._rebuild_table()
            self.assertEqual(window.recent_mail_table.rowCount(), 0)
            self.assertFalse(window.recent_empty_label.isHidden())
            self.assertFalse(window.recent_mail_table.isHidden())

            window.results[key] = FetchResult(
                "found",
                code="654321",
                sender="Old sender",
                subject="Old subject",
                received="Old time",
                messages=(MailMessage("654321", "Old subject", "Old sender", "Old time", "Old"),),
            )
            window._rebuild_table()
            window.table.selectRow(0)
            self.assertEqual(window.recent_mail_table.rowCount(), 1)

            with patch.object(window.thread_pool, "start"):
                window._start_fetch([account])
            self.assertNotIn(key, window.results)
            self.assertEqual(window.recent_mail_table.rowCount(), 0)
            self.assertEqual(window.table.item(0, window.COL_SENDER).text(), "—")
            self.assertEqual(window.table.item(0, window.COL_SUBJECT).text(), "Checking…")
            window._stop_fetch()
        finally:
            window.close()

    def test_clicking_recent_mail_code_copies_exact_code_and_shows_popup(self):
        window = app_module.MainWindow()
        try:
            account = Account("click-copy@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found",
                code="246810",
                messages=(
                    MailMessage("246810", "Code mail", "Sender", "Now", "Body"),
                    MailMessage("", "Regular mail", "Sender", "Earlier", "Body"),
                ),
            )
            window._rebuild_table()

            self.app.clipboard().setText("unchanged")
            with patch.object(app_module.QToolTip, "showText") as show_popup:
                window.recent_mail_table.cellClicked.emit(0, window.MAIL_COL_CODE)

            self.assertEqual(self.app.clipboard().text(), "246810")
            show_popup.assert_called_once()
            self.assertEqual(show_popup.call_args.args[1], "Copied: 246810")

            self.app.clipboard().setText("unchanged")
            with patch.object(app_module.QToolTip, "showText") as show_popup:
                window.recent_mail_table.cellClicked.emit(1, window.MAIL_COL_CODE)
            self.assertEqual(self.app.clipboard().text(), "unchanged")
            show_popup.assert_not_called()

            with patch.object(app_module.MessageBodyDialog, "exec") as execute:
                window.recent_mail_table.cellDoubleClicked.emit(0, window.MAIL_COL_CODE)
            execute.assert_called_once()
        finally:
            window.close()

    def test_copy_selected_code_prefers_recent_mail(self):
        window = app_module.MainWindow()
        try:
            account = Account("copy@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "found",
                code="111111",
                messages=(MailMessage("999999", "Recent", "Sender", "Now", "Body"),),
            )
            window._rebuild_table()
            window.table.selectRow(0)
            window.recent_mail_table.selectRow(0)
            self.assertEqual(window._selected_code(), "999999")
            window.copy_button.click()
            self.assertEqual(self.app.clipboard().text(), "999999")

            window.recent_mail_table.clearSelection()
            self.assertEqual(window._selected_code(), "111111")
            window.copy_button.click()
            self.assertEqual(self.app.clipboard().text(), "111111")
        finally:
            window.close()

    def test_message_body_dialog_is_plain_read_only_text(self):
        account = Account("demo@yandex.com", "fake", "yandex")
        result = FetchResult(
            "found",
            code="482910",
            subject="Your code",
            sender="Security <security@example.com>",
            received="Now",
            body="Hello <b>literal text</b>\nCode: 482910",
        )
        dialog = app_module.MessageBodyDialog(account, result, "Yandex Mail")
        try:
            self.assertTrue(dialog.body.isReadOnly())
            self.assertEqual(dialog.body.toPlainText(), result.body)
        finally:
            dialog.close()

    def test_double_clicking_account_column_opens_fetched_message_body(self):
        window = app_module.MainWindow()
        try:
            account = Account(
                "demo@yandex.com",
                "fake",
                "yandex",
                mail_alias="demo+100teamA@yandex.com",
            )
            result = FetchResult("found", subject="Verification", body="Your code is 482910")
            window.accounts = [account]
            window.results[account.email.casefold()] = result
            window._rebuild_table()
            with patch.object(
                app_module.MessageBodyDialog,
                "exec",
                return_value=QDialog.DialogCode.Accepted,
            ) as execute:
                window._cell_double_clicked(0, window.COL_EMAIL)
            execute.assert_called_once()
        finally:
            window.close()

    def test_double_click_does_not_open_when_body_is_unavailable(self):
        window = app_module.MainWindow()
        try:
            account = Account("demo@outlook.com", "", "microsoft")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "not_found", subject="Metadata only", body=""
            )
            window._rebuild_table()
            with patch.object(app_module.MessageBodyDialog, "exec") as execute:
                window._cell_double_clicked(0, window.COL_SUBJECT)
            execute.assert_not_called()
        finally:
            window.close()

    def test_column_zoom_and_fit_keep_manual_resize_mode(self):
        window = app_module.MainWindow()
        try:
            window.show()
            self.app.processEvents()
            self.assertEqual(window.table.columnCount(), 8)
            self.assertEqual(len(window.COLUMN_DEFAULT_WIDTHS), window.table.columnCount())
            self.assertEqual(len(window.COLUMN_MIN_WIDTHS), window.table.columnCount())
            self.assertEqual(
                [
                    window.table.horizontalHeaderItem(column).text()
                    for column in range(window.table.columnCount())
                ],
                ["", "ACCOUNT", "PROVIDER", "SECRET", "SENDER", "SUBJECT", "RECEIVED", "STATUS"],
            )
            self.assertEqual(window.content_splitter.count(), 2)
            self.assertIs(window.content_splitter.widget(0), window.table)
            self.assertIs(window.content_splitter.widget(1), window.recent_mail_panel)
            self.assertFalse(window.content_splitter.childrenCollapsible())
            self.assertEqual(window.recent_mail_title.text(), "All Mail")
            self.assertEqual(window.recent_mail_table.columnCount(), 5)
            self.assertEqual(
                [
                    window.recent_mail_table.horizontalHeaderItem(column).text()
                    for column in range(window.recent_mail_table.columnCount())
                ],
                ["YANDEX SUBMAIL", "FACEBOOK NAME", "CODE MAIL", "SUBJECT", "RECEIVED"],
            )
            self.assertFalse(window.recent_mail_table.isSortingEnabled())
            self.assertEqual(
                len(window.MAIL_COLUMN_DEFAULT_WIDTHS),
                window.recent_mail_table.columnCount(),
            )
            self.assertEqual(
                len(window.MAIL_COLUMN_MIN_WIDTHS),
                window.recent_mail_table.columnCount(),
            )
            header = window.table.horizontalHeader()
            mail_header = window.recent_mail_table.horizontalHeader()
            header.resizeSection(window.COL_EMAIL, 200)
            mail_header.resizeSection(window.MAIL_COL_ACCOUNT, 200)
            before = header.sectionSize(window.COL_EMAIL)
            mail_before = mail_header.sectionSize(window.MAIL_COL_ACCOUNT)
            window._zoom_columns(1.16)
            self.assertGreater(header.sectionSize(window.COL_EMAIL), before)
            self.assertGreater(
                mail_header.sectionSize(window.MAIL_COL_ACCOUNT),
                mail_before,
            )
            self.assertEqual(
                header.sectionResizeMode(window.COL_EMAIL),
                QHeaderView.ResizeMode.Interactive,
            )
            self.assertEqual(
                header.sectionResizeMode(window.COL_CHECK),
                QHeaderView.ResizeMode.Fixed,
            )
            window._fit_columns()
            for column in range(1, window.table.columnCount()):
                self.assertGreaterEqual(
                    header.sectionSize(column),
                    window.COLUMN_MIN_WIDTHS[column],
                )
                self.assertEqual(
                    header.sectionResizeMode(column),
                    QHeaderView.ResizeMode.Interactive,
                )
            for column in range(window.recent_mail_table.columnCount()):
                self.assertGreaterEqual(
                    mail_header.sectionSize(column),
                    window.MAIL_COLUMN_MIN_WIDTHS[column],
                )
                self.assertEqual(
                    mail_header.sectionResizeMode(column),
                    QHeaderView.ResizeMode.Interactive,
                )

            account = Account("widths@yandex.com", "fake", "yandex")
            window.accounts = [account]
            window.results[account.email.casefold()] = FetchResult(
                "not_found",
                messages=(MailMessage("", "A message", "Sender", "Now", "Body"),),
            )
            window._rebuild_table()
            self.app.processEvents()
            mail_header.resizeSection(window.MAIL_COL_SUBJECT, 444)
            window._rebuild_recent_mail()
            self.app.processEvents()
            self.assertEqual(mail_header.sectionSize(window.MAIL_COL_SUBJECT), 444)
        finally:
            window.close()

    def test_hotmail_workspace_ui_and_functionality(self):
        window = app_module.MainWindow()
        try:
            window._switch_view(window.VIEW_MICROSOFT, announce=False)
            self.assertFalse(window.hotmail_container.isHidden())
            self.assertTrue(window.yandex_container.isHidden())

            # Test text edit dynamic badge count
            self.assertEqual(window.hotmail_accounts_badge.text(), "Accounts: 0/1000")
            window.hotmail_text_edit.setPlainText("user1@outlook.com|pass|token1|client1\nuser2@hotmail.com|pass|token2|client2")
            self.assertEqual(window.hotmail_accounts_badge.text(), "Accounts: 2/1000")

            # Test table population on completed task
            result = FetchResult("found", code="654321", subject="Your security code", sender="Microsoft", received="12:00")
            window._start_hotmail_fetch()
            self.assertEqual(window.hotmail_results_table.rowCount(), 2)

            window._on_hotmail_task_completed(window._hotmail_run_id, 0, "user1@outlook.com", result)
            code_item = window.hotmail_results_table.item(0, 3)
            self.assertIsNotNone(code_item)
            self.assertEqual(code_item.text(), "654321")

            # Test double click to copy
            window._on_hotmail_table_double_clicked(0, 3)
            self.assertEqual(self.app.clipboard().text(), "654321")

            # Test copy all codes
            window._copy_all_hotmail_codes()
            self.assertEqual(self.app.clipboard().text(), "654321")

            # Test filter
            window._filter_hotmail_results("user1")
            self.assertFalse(window.hotmail_results_table.isRowHidden(0))
            self.assertTrue(window.hotmail_results_table.isRowHidden(1))

            # Test clear
            window._clear_hotmail_input()
            self.assertEqual(window.hotmail_text_edit.toPlainText(), "")
            self.assertEqual(window.hotmail_results_table.rowCount(), 0)
            self.assertEqual(window.hotmail_accounts_badge.text(), "Accounts: 0/1000")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
