"""Desktop UI for reading verification codes and managing Yandex mail."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading

from PySide6.QtCore import (
    QObject,
    QPoint,
    QPropertyAnimation,
    QRunnable,
    QStandardPaths,
    QThreadPool,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app_settings import load_settings, save_settings, valid_client_id
from microsoft_oauth import (
    MSAL_AVAILABLE,
    MicrosoftOAuthManager,
    OAuthError,
    get_access_token_from_refresh_token,
)
from secure_store import SecureStoreError, load_accounts, mask_password, save_accounts
from totp import InvalidTOTPSecret, generate_totp, totp_seconds_remaining
from updater import (
    APP_VERSION,
    UpdateInfo,
    apply_update_and_restart,
    check_for_updates,
    download_update_archive,
)
from yandex_mail import (
    Account,
    DeleteMailResult,
    FetchResult,
    MailMessage,
    delete_all_yandex_mail,
    detect_provider,
    fetch_latest_code,
    parse_account_lines,
    provider_label,
)


APP_NAME = "Theara"
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
IS_COMPILED = getattr(sys, "frozen", False) or "__compiled__" in globals() or hasattr(sys, "_MEIPASS")
APP_BINARY_DIR = (
    Path(sys.executable).resolve().parent
    if IS_COMPILED
    else SOURCE_DIR
)
LEGACY_DATA_DIR = APP_BINARY_DIR


def ensure_desktop_shortcut() -> None:
    """Create a desktop shortcut to Theara.exe if running on Windows and not yet present."""
    if sys.platform != "win32":
        return
    try:
        exe_path = Path(sys.executable).resolve()
        if not exe_path.name.lower().endswith(".exe") or "python" in exe_path.name.lower():
            candidate = APP_BINARY_DIR / "Theara.exe"
            if candidate.is_file():
                exe_path = candidate
            else:
                return

        # Find Desktop directory accurately
        desktop_str = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        desktop = Path(desktop_str) if desktop_str else (Path.home() / "Desktop")
        if not desktop.is_dir():
            desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if not desktop.is_dir():
            return
        shortcut = desktop / "Theara.lnk"

        # Method 1: Using Windows COM (fastest and most reliable)
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            sc = shell.CreateShortcut(str(shortcut))
            sc.TargetPath = str(exe_path)
            sc.WorkingDirectory = str(exe_path.parent)
            sc.IconLocation = f"{exe_path},0"
            sc.Description = "Theara - Mail & 2FA Code Tool"
            sc.Save()
            return
        except Exception:
            pass

        # Method 2: VBS script execution (built into all Windows versions)
        try:
            vbs_code = (
                f'Set ws = CreateObject("WScript.Shell")\r\n'
                f'Set sc = ws.CreateShortcut("{shortcut}")\r\n'
                f'sc.TargetPath = "{exe_path}"\r\n'
                f'sc.WorkingDirectory = "{exe_path.parent}"\r\n'
                f'sc.IconLocation = "{exe_path},0"\r\n'
                f'sc.Description = "Theara - Mail & 2FA Code Tool"\r\n'
                f'sc.Save\r\n'
            )
            temp_vbs = Path(tempfile.gettempdir()) / "create_theara_lnk.vbs"
            temp_vbs.write_text(vbs_code, encoding="utf-8")
            import subprocess
            subprocess.run(
                ["wscript.exe", str(temp_vbs)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
            temp_vbs.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception:
        pass


def _default_data_dir() -> Path:
    """Return a stable per-user directory for writable application state."""

    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]) / APP_NAME.casefold()
    return Path.home() / f".{APP_NAME.casefold()}"


APP_DIR = _default_data_dir()
ACCOUNT_STORE = APP_DIR / "accounts.secure.json"
SETTINGS_FILE = APP_DIR / "settings.json"
MICROSOFT_TOKEN_CACHE = APP_DIR / "microsoft_token_cache.secure.json"
APP_ICON = RESOURCE_DIR / "assets" / "theara.ico"
BUNDLED_KHMER_FONT = RESOURCE_DIR / "assets" / "fonts" / "NotoSansKhmer.ttf"
_BUNDLED_KHMER_FONT_ID: int | None = None
_BUNDLED_KHMER_FAMILIES: tuple[str, ...] = ()


def contains_khmer(text: object) -> bool:
    """Return whether text contains Khmer script or Khmer symbols."""

    return any(
        0x1780 <= ord(character) <= 0x17FF
        or 0x19E0 <= ord(character) <= 0x19FF
        for character in str(text or "")
    )


def configure_application_fonts(
    application: QApplication | None = None,
) -> tuple[str, ...]:
    """Register the bundled Khmer font and configure reliable UI fallbacks."""

    global _BUNDLED_KHMER_FONT_ID, _BUNDLED_KHMER_FAMILIES

    application = application or QApplication.instance()
    if application is None:
        return _BUNDLED_KHMER_FAMILIES

    if _BUNDLED_KHMER_FONT_ID is None:
        _BUNDLED_KHMER_FONT_ID = (
            QFontDatabase.addApplicationFont(str(BUNDLED_KHMER_FONT))
            if BUNDLED_KHMER_FONT.is_file()
            else -1
        )
        if _BUNDLED_KHMER_FONT_ID >= 0:
            registered_families = QFontDatabase.applicationFontFamilies(
                _BUNDLED_KHMER_FONT_ID
            )
            if registered_families:
                _BUNDLED_KHMER_FAMILIES = (registered_families[0],)

    family_order = list(
        dict.fromkeys(
            (
                "Segoe UI",
                *_BUNDLED_KHMER_FAMILIES,
                "Khmer UI",
                "Khmer OS System",
                "Segoe UI Emoji",
            )
        )
    )
    application_font = QFont(application.font())
    application_font.setFamilies(family_order)
    application.setFont(application_font)
    return _BUNDLED_KHMER_FAMILIES


def khmer_aware_font(base_font: QFont) -> QFont:
    """Prefer the bundled font for text that contains Khmer shaping marks."""

    bundled_families = configure_application_fonts()
    family_order = list(
        dict.fromkeys(
            (
                "Segoe UI",
                *bundled_families,
                "Khmer UI",
                "Khmer OS System",
                "Segoe UI Emoji",
            )
        )
    )
    font = QFont(base_font)
    font.setFamilies(family_order)
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() + 1.0)
    return font


def apply_khmer_font(target, text: object) -> None:
    """Apply Khmer-capable font families to a Qt widget or item when needed."""

    if contains_khmer(text):
        target.setFont(khmer_aware_font(target.font()))


def migrate_legacy_app_data() -> int:
    """Copy legacy adjacent data once without bundling or deleting secrets."""

    migrated = 0
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return migrated
    for destination in (ACCOUNT_STORE, SETTINGS_FILE, MICROSOFT_TOKEN_CACHE):
        source = LEGACY_DATA_DIR / destination.name
        temporary = destination.with_name(destination.name + ".migrating")
        try:
            if destination.exists() or not source.is_file():
                continue
            if source.resolve() == destination.resolve():
                continue
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            migrated += 1
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return migrated


STYLE = """
QMainWindow, QDialog, QWidget#root {
    background-color: #070B14;
    color: #F8FAFC;
}
QWidget {
    color: #F8FAFC;
    font-size: 12px;
}
QFrame#header {
    background-color: #0B1120;
    border-bottom: 1px solid #1E293B;
}
QFrame#modeNav {
    background-color: #0B1120;
    border-bottom: 1px solid #1E293B;
}
QFrame#appFooter {
    background-color: #0B1120;
    border-top: 1px solid #1E293B;
}
QLabel#appMark {
    color: #070B14;
    background-color: #00D8F6;
    border-radius: 20px;
    font-size: 20px;
    font-weight: 900;
}
QLabel#title {
    color: #F8FAFC;
    font-size: 22px;
    font-weight: 700;
}
QLabel#subtitle, QLabel#muted, QLabel#cardCaption {
    color: #94A3B8;
}
QLabel#footerCredit {
    color: #38BDF8;
    font-size: 11px;
    font-weight: 600;
}
QLabel#footerCopyright {
    color: #64748B;
    font-size: 10px;
}
QLabel#twoFactorEyebrow {
    color: #38BDF8;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1px;
}
QLabel#twoFactorTitle {
    color: #F8FAFC;
    font-size: 24px;
    font-weight: 800;
}
QLabel#twoFactorCode {
    color: #00E5FF;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 40px;
    font-weight: 900;
    letter-spacing: 2px;
}
QLabel#twoFactorError { color: #F87171; font-size: 12px; }
QFrame#card {
    background-color: #0F172A;
    border: 1px solid #1E293B;
    border-radius: 8px;
}
QFrame#twoFactorCard {
    background-color: #0F172A;
    border: 1.5px solid #0284C7;
    border-radius: 14px;
}
QFrame#sectionCard {
    background-color: #0B1120;
    border: 1px solid #1E293B;
    border-radius: 10px;
}
QLabel#sectionTitle {
    color: #F8FAFC;
    font-size: 15px;
    font-weight: 700;
}
QLabel#accountsPill {
    color: #38BDF8;
    background-color: #0F2237;
    border: 1px solid #0284C7;
    border-radius: 12px;
    padding: 3px 12px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#cardValue {
    color: #00D8F6;
    font-size: 22px;
    font-weight: 700;
}
QPushButton {
    min-height: 31px;
    padding: 0 14px;
    color: #E2E8F0;
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 6px;
    font-weight: 600;
}
QPushButton:hover {
    color: #F8FAFC;
    background-color: #27354A;
    border-color: #0284C7;
}
QPushButton:pressed { background-color: #16202E; }
QPushButton:disabled {
    color: #475569;
    background-color: #0B1120;
    border-color: #1E293B;
}
QPushButton#primary {
    color: #070B14;
    background-color: #00D8F6;
    border-color: #38BDF8;
    font-weight: 800;
}
QPushButton#primary:hover {
    background-color: #38E1FF;
    border-color: #7DD3FC;
}
QPushButton#startCyan {
    color: #070B14;
    background-color: #00C4DF;
    border: 1px solid #00E5FF;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 800;
    min-height: 38px;
}
QPushButton#startCyan:hover {
    background-color: #26E0FA;
    border-color: #7CEBFF;
}
QPushButton#startCyan:pressed {
    background-color: #00A6BE;
}
QPushButton#startCyan:disabled {
    color: #475569;
    background-color: #0F2237;
    border-color: #1E3A5F;
}
QPushButton#danger {
    color: #FCA5A5;
    background-color: #241416;
    border-color: #7F1D1D;
}
QPushButton#danger:hover {
    background-color: #381A1D;
    border-color: #DC2626;
}
QPushButton#modeSwitch {
    min-height: 34px;
    min-width: 155px;
    padding: 0 18px;
    color: #94A3B8;
    background-color: #10192A;
    border: 1px solid #1E293B;
    border-radius: 7px;
    font-weight: 700;
}
QPushButton#modeSwitch:hover {
    color: #F8FAFC;
    background-color: #17253D;
    border-color: #0284C7;
}
QPushButton#modeSwitch:checked {
    color: #070B14;
    background-color: #00D8F6;
    border-color: #38BDF8;
    font-weight: 800;
}
QPushButton#modeSwitch:disabled {
    color: #475569;
    background-color: #0B1120;
    border-color: #1E293B;
}
QLineEdit, QPlainTextEdit, QComboBox {
    color: #F8FAFC;
    selection-background-color: #0284C7;
    background-color: #090F1D;
    border: 1px solid #1E293B;
    border-radius: 6px;
    padding: 6px 10px;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
    border-color: #00D8F6;
}
QPlainTextEdit#codeEditor {
    font-family: "Consolas", "Courier New", monospace;
    font-size: 13px;
    line-height: 1.4;
    background-color: #070B14;
    border: 1.5px solid #00D8F6;
    border-radius: 8px;
    padding: 10px;
}
QPlainTextEdit#codeEditor:focus {
    border: 1.5px solid #38E1FF;
}
QComboBox QAbstractItemView {
    color: #F8FAFC;
    background-color: #0F172A;
    selection-background-color: #0284C7;
}
QProgressBar {
    min-height: 8px;
    max-height: 8px;
    color: transparent;
    background-color: #070B14;
    border: 1px solid #1E293B;
    border-radius: 4px;
}
QProgressBar::chunk {
    background-color: #00D8F6;
    border-radius: 3px;
}
QTableWidget {
    color: #F1F5F9;
    background-color: #090F1D;
    alternate-background-color: #0D1527;
    border: 1px solid #1E293B;
    border-radius: 8px;
    gridline-color: #162238;
    selection-background-color: #0E365E;
    selection-color: #FFFFFF;
}
QHeaderView {
    background-color: #0F172A;
    border: none;
}
QHeaderView::section {
    color: #38BDF8;
    background-color: #0F172A;
    border: none;
    border-right: 1px solid #1E293B;
    border-bottom: 1px solid #0284C7;
    padding: 8px 6px;
    font-weight: 700;
}
QTableCornerButton::section {
    background-color: #0F172A;
    border: none;
}
QScrollBar:vertical {
    background-color: #070B14;
    width: 10px;
    margin: 0px;
    border: none;
}
QScrollBar::handle:vertical {
    background-color: #1E293B;
    min-height: 24px;
    border-radius: 5px;
    margin: 2px;
}
QScrollBar::handle:vertical:hover {
    background-color: #334155;
}
QScrollBar:horizontal {
    background-color: #070B14;
    height: 10px;
    margin: 0px;
    border: none;
}
QScrollBar::handle:horizontal {
    background-color: #1E293B;
    min-width: 24px;
    border-radius: 5px;
    margin: 2px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #334155;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0px;
    height: 0px;
    border: none;
    background: none;
}
QCheckBox { spacing: 7px; color: #CBD5E1; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    background-color: #090F1D;
    border: 1px solid #334155;
    border-radius: 3px;
}
QCheckBox::indicator:hover { border-color: #00D8F6; }
QCheckBox::indicator:checked { background-color: #00D8F6; border-color: #00D8F6; }
QStatusBar {
    color: #94A3B8;
    background-color: #0B1120;
    border-top: 1px solid #1E293B;
}
QToolTip {
    color: #F8FAFC;
    background-color: #0F172A;
    border: 1px solid #0284C7;
    padding: 6px 10px;
    border-radius: 5px;
}
QMenu {
    background-color: #0F172A;
    border: 1px solid #1E293B;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
    color: #F1F5F9;
}
QMenu::item:selected {
    background-color: #0284C7;
    color: #FFFFFF;
}
QMenu::separator {
    height: 1px;
    background-color: #1E293B;
    margin: 4px 6px;
}
"""


def _button(text: str, object_name: str = "") -> QPushButton:
    button = QPushButton(text)
    if object_name:
        button.setObjectName(object_name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def validate_mail_alias(account: Account, alias: str) -> tuple[bool, str]:
    """Validate an optional Yandex plus-address for exact recipient preference."""
    candidate = str(alias or "").strip()
    if not candidate:
        return True, ""
    resolved = detect_provider(account.email) if account.provider == "auto" else account.provider
    if resolved != "yandex":
        return False, "Recipient aliases are currently supported for Yandex rows only."
    if any(character.isspace() for character in candidate) or "|" in candidate:
        return False, "The alias cannot contain spaces or | characters."
    login_local, login_separator, login_domain = account.email.strip().rpartition("@")
    alias_local, alias_separator, alias_domain = candidate.rpartition("@")
    if not login_separator or not alias_separator or not alias_local or not alias_domain:
        return False, "Enter a complete Yandex alias address."
    if login_domain.casefold() != alias_domain.casefold():
        return False, "The alias must use the same domain as the login mailbox."
    login_base, login_plus, _login_tag = login_local.rpartition("+")
    if not login_plus:
        login_base = login_local
    alias_base, alias_plus, alias_tag = alias_local.rpartition("+")
    if not alias_plus or not alias_tag:
        return False, "The Yandex alias must contain a non-empty +tag."
    if alias_base.casefold() != login_base.casefold():
        return False, "The alias must belong to the same base mailbox as the login."
    return True, ""


class BulkImportDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paste mail accounts")
        self.resize(650, 430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        title = QLabel("Add mail accounts")
        title.setObjectName("title")
        layout.addWidget(title)
        note = QLabel(
            "Use email|password for automatic detection, or provider|email|password for a custom "
            "Zoho mailbox. Microsoft accounts must be added with the Add button and connected "
            "through OAuth. Saved secrets are encrypted for your current Windows user."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "first@yandex.com|mail-app-password\nname@zoho.com|zoho-app-password\n"
            "zoho|name@company.com|zoho-app-password"
        )
        layout.addWidget(self.editor, 1)

        actions = QHBoxLayout()
        paste = _button("Paste from clipboard")
        paste.clicked.connect(self._paste)
        actions.addWidget(paste)
        actions.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add accounts")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)

    def _paste(self) -> None:
        self.editor.setPlainText(QGuiApplication.clipboard().text())

    def text(self) -> str:
        return self.editor.toPlainText()


class ProviderAccountDialog(QDialog):
    """Provider-aware account entry; Microsoft intentionally has no password field."""

    PROVIDERS = (
        ("Auto-detect from email", "auto", ""),
        ("Yandex Mail", "yandex", ""),
        ("Zoho personal/free - Global (imap.zoho.com)", "zoho", "imap.zoho.com"),
        ("Zoho organization - US (imappro.zoho.com)", "zoho", "imappro.zoho.com"),
        ("Zoho personal - Europe (imap.zoho.eu)", "zoho", "imap.zoho.eu"),
        ("Zoho organization - Europe (imappro.zoho.eu)", "zoho", "imappro.zoho.eu"),
        ("Zoho personal - India (imap.zoho.in)", "zoho", "imap.zoho.in"),
        ("Zoho organization - India (imappro.zoho.in)", "zoho", "imappro.zoho.in"),
        ("Zoho - Australia (imap.zoho.com.au)", "zoho", "imap.zoho.com.au"),
        ("Zoho - Japan (imap.zoho.jp)", "zoho", "imap.zoho.jp"),
        ("Zoho - China (imap.zoho.com.cn)", "zoho", "imap.zoho.com.cn"),
        ("Microsoft Outlook / Hotmail", "microsoft", ""),
    )

    def __init__(
        self,
        parent: QWidget | None = None,
        preferred_provider: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add mail account")
        self.setMinimumWidth(570)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(9)
        title = QLabel("Add one mailbox")
        title.setObjectName("title")
        layout.addWidget(title)

        layout.addWidget(QLabel("Provider and mail server"))
        self.provider = QComboBox()
        for label, provider, host in self.PROVIDERS:
            self.provider.addItem(label, (provider, host))
        if preferred_provider:
            for index in range(self.provider.count()):
                provider, _host = self.provider.itemData(index)
                if provider == preferred_provider:
                    self.provider.setCurrentIndex(index)
                    break
        layout.addWidget(self.provider)

        layout.addWidget(QLabel("Email"))
        self.email = QLineEdit()
        self.email.setPlaceholderText("name@example.com")
        layout.addWidget(self.email)

        self.alias_label = QLabel("Yandex plus alias (optional)")
        self.mail_alias = QLineEdit()
        self.mail_alias.setPlaceholderText("name+100teamA@yandex.com")
        layout.addWidget(self.alias_label)
        layout.addWidget(self.mail_alias)

        self.password_label = QLabel("Password / mail app password")
        layout.addWidget(self.password_label)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Provider password or mail app password")
        layout.addWidget(self.password)
        self.show_password = QCheckBox("Show password")
        self.show_password.toggled.connect(
            lambda checked: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(self.show_password)

        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add account")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.email.textChanged.connect(self._provider_changed)
        self._provider_changed()

    def _provider_changed(self) -> None:
        provider, host = self.provider.currentData()
        detected = detect_provider(self.email.text().strip()) if provider == "auto" else provider
        is_microsoft = detected == "microsoft"
        is_yandex = detected == "yandex"
        self.password.setEnabled(not is_microsoft)
        self.password_label.setEnabled(not is_microsoft)
        self.show_password.setEnabled(not is_microsoft)
        self.alias_label.setVisible(is_yandex)
        self.mail_alias.setVisible(is_yandex)
        if is_microsoft:
            self.password.clear()
            self.note.setText(
                "Microsoft no longer accepts IMAP passwords or app passwords. Add the mailbox, then "
                "use Connect Microsoft to complete OAuth sign-in."
            )
        elif detected == "zoho":
            server_text = host or "automatic Zoho regional default"
            self.note.setText(
                f"Server: {server_text}. Enable IMAP in Zoho first. With 2FA, use the Zoho app-specific "
                "password. Match the server shown in Zoho's Server Configuration Details."
            )
        elif detected == "yandex":
            self.note.setText("Use a Yandex Mail app password and enable IMAP access in Mail settings.")
        else:
            self.note.setText("Yandex, standard Zoho, Outlook, Hotmail, and Live domains are detected automatically.")

    def _accept_if_valid(self) -> None:
        provider, _ = self.provider.currentData()
        address = self.email.text().strip()
        if "@" not in address:
            QMessageBox.warning(self, "Missing account details", "Enter a valid email address.")
            return
        resolved = detect_provider(address) if provider == "auto" else provider
        if resolved == "unsupported":
            QMessageBox.warning(
                self,
                "Choose a provider",
                "This custom domain cannot be detected. Select Yandex, the correct Zoho server, or Microsoft.",
            )
            return
        if resolved != "microsoft" and not self.password.text():
            QMessageBox.warning(self, "Missing account details", "Enter a password or mail app password.")
            return
        stored_provider = "microsoft" if resolved == "microsoft" else str(provider)
        candidate = Account(
            address,
            "" if resolved == "microsoft" else self.password.text(),
            stored_provider,
            str(self.provider.currentData()[1]),
            self.mail_alias.text().strip() if resolved == "yandex" else "",
        )
        valid_alias, alias_error = validate_mail_alias(candidate, candidate.mail_alias)
        if not valid_alias:
            QMessageBox.warning(self, "Invalid Yandex alias", alias_error)
            return
        self.accept()

    def account(self) -> Account:
        provider, host = self.provider.currentData()
        resolved = detect_provider(self.email.text().strip()) if provider == "auto" else provider
        stored_provider = "microsoft" if resolved == "microsoft" else str(provider)
        return Account(
            self.email.text().strip(),
            "" if resolved == "microsoft" else self.password.text(),
            stored_provider,
            str(host),
            self.mail_alias.text().strip() if resolved == "yandex" else "",
        )


class MicrosoftSetupDialog(QDialog):
    REGISTRATION_URL = "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app"

    def __init__(self, client_id: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Microsoft OAuth setup")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(9)
        title = QLabel("Configure Microsoft sign-in")
        title.setObjectName("title")
        layout.addWidget(title)
        explanation = QLabel(
            "Create a Microsoft Entra public-client app for personal Microsoft accounts, enable public "
            "client flows, and add delegated IMAP.AccessAsUser.All permission. Paste its Application "
            "(client) ID below. A client ID is public and is not a client secret."
        )
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addWidget(QLabel("Application (client) ID"))
        self.client_id = QLineEdit(client_id)
        self.client_id.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        layout.addWidget(self.client_id)
        open_docs = _button("Open Microsoft registration guide")
        open_docs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.REGISTRATION_URL)))
        layout.addWidget(open_docs, alignment=Qt.AlignmentFlag.AlignLeft)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if not valid_client_id(self.client_id.text()):
            QMessageBox.warning(self, "Invalid client ID", "Enter the GUID shown as Application (client) ID.")
            return
        self.accept()


class DeviceCodeDialog(QDialog):
    def __init__(self, flow: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect Microsoft mailbox")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        title = QLabel("Finish sign-in with Microsoft")
        title.setObjectName("title")
        layout.addWidget(title)
        note = QLabel("Open Microsoft's sign-in page, enter this one-time code, then sign in to the mailbox you added.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        code = QLabel(str(flow.get("user_code", "")))
        code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code.setStyleSheet("font: bold 25px Consolas; color: #f5c542; padding: 14px;")
        layout.addWidget(code)
        actions = QHBoxLayout()
        open_page = _button("Open Microsoft sign-in", "primary")
        open_page.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(str(flow.get("verification_uri", "https://microsoft.com/devicelogin")))
            )
        )
        copy_code = _button("Copy code")
        copy_code.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(str(flow.get("user_code", "")))
        )
        actions.addWidget(open_page)
        actions.addWidget(copy_code)
        layout.addLayout(actions)
        self.status = QLabel("Waiting for Microsoft sign-in…")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel.rejected.connect(self.reject)
        layout.addWidget(cancel)


class MessageBodyDialog(QDialog):
    """Read-only viewer for the decoded body of a fetched verification email."""

    def __init__(
        self,
        account: Account,
        result: FetchResult | MailMessage,
        provider_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        configure_application_fonts()
        subject = str(getattr(result, "subject", "") or "Message")
        safe_title = " ".join(subject.split())[:90]
        self.setWindowTitle(f"Message - {safe_title}")
        self.resize(900, 650)
        self.setMinimumSize(640, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(9)
        title = QLabel(subject)
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        apply_khmer_font(title, subject)
        layout.addWidget(title)

        metadata_lines = [
            f"Account: {account.email}",
            f"Provider: {provider_name}",
        ]
        recipient = str(getattr(result, "recipient", "") or account.mail_alias or "")
        facebook_name = str(getattr(result, "facebook_name", "") or "")
        if recipient:
            metadata_lines.append(f"Yandex submail: {recipient}")
        if facebook_name:
            metadata_lines.append(f"Facebook name: {facebook_name}")
        metadata_lines.extend(
            [
                f"From: {getattr(result, 'sender', '') or 'Unknown sender'}",
                f"Received: {getattr(result, 'received', '') or 'Unknown time'}",
            ]
        )
        metadata_text = "\n".join(metadata_lines)
        metadata = QLabel(metadata_text)
        metadata.setObjectName("muted")
        metadata.setTextFormat(Qt.TextFormat.PlainText)
        metadata.setWordWrap(True)
        metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        apply_khmer_font(metadata, metadata_text)
        layout.addWidget(metadata)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        body_text = str(getattr(result, "body", "") or "")
        self.body.setPlainText(body_text or "No readable text body was available for this message.")
        apply_khmer_font(self.body, body_text)
        self._base_body_font = QFont(self.body.font())
        cursor = self.body.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.body.setTextCursor(cursor)
        layout.addWidget(self.body, 1)

        actions = QHBoxLayout()
        zoom_out = _button("Text -")
        zoom_in = _button("Text +")
        copy_body = _button("Copy body")
        close_button = _button("Close", "primary")
        zoom_out.clicked.connect(lambda: self.body.zoomOut(1))
        zoom_in.clicked.connect(lambda: self.body.zoomIn(1))
        copy_body.clicked.connect(lambda: QGuiApplication.clipboard().setText(body_text))
        copy_body.setEnabled(bool(body_text))
        close_button.clicked.connect(self.accept)
        actions.addWidget(zoom_out)
        actions.addWidget(zoom_in)
        actions.addWidget(copy_body)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self._shortcuts = [
            QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.body.zoomIn(1)),
            QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self.body.zoomIn(1)),
            QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.body.zoomOut(1)),
            QShortcut(QKeySequence("Ctrl+0"), self, activated=self._reset_text_zoom),
        ]

    def _reset_text_zoom(self) -> None:
        self.body.setFont(QFont(self._base_body_font))


class TaskSignals(QObject):
    completed = Signal(int, str, object)


class FetchTask(QRunnable):
    def __init__(
        self,
        run_id: int,
        account: Account,
        max_age_hours: int,
        cancel_event: threading.Event,
        access_token_provider=None,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self.account = account
        self.max_age_hours = max_age_hours
        self.cancel_event = cancel_event
        self.access_token_provider = access_token_provider
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        if self.cancel_event.is_set():
            return
        try:
            result = fetch_latest_code(
                self.account,
                max_messages=0,
                max_age_hours=self.max_age_hours,
                timeout=18,
                access_token_provider=self.access_token_provider,
            )
        except Exception:
            result = FetchResult(
                status="connection_error",
                detail="Unexpected mail error. No password was logged.",
            )
        if not self.cancel_event.is_set():
            self.signals.completed.emit(self.run_id, self.account.email, result)


class HotmailTaskSignals(QObject):
    completed = Signal(int, int, str, object)  # run_id, row_index, email, result


class HotmailFetchTask(QRunnable):
    """Worker task for fetching verification codes from a Hotmail/Outlook account."""

    def __init__(
        self,
        run_id: int,
        row_index: int,
        raw_line: str,
        max_age_hours: int,
        cancel_event: threading.Event,
        msal_token_func=None,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self.row_index = row_index
        self.raw_line = raw_line.strip()
        self.max_age_hours = max_age_hours
        self.cancel_event = cancel_event
        self.msal_token_func = msal_token_func
        self.signals = HotmailTaskSignals()

    @Slot()
    def run(self) -> None:
        if self.cancel_event.is_set():
            return
        parts = [p.strip() for p in self.raw_line.split("|")]
        email_addr = parts[0] if parts else ""
        password = parts[1] if len(parts) > 1 else ""
        refresh_token = parts[2] if len(parts) > 2 else ""
        client_id = parts[3] if len(parts) > 3 else ""

        if len(parts) == 2 and len(parts[1]) > 40:
            refresh_token = parts[1]
            password = ""

        if not email_addr or "@" not in email_addr:
            if not self.cancel_event.is_set():
                self.signals.completed.emit(
                    self.run_id,
                    self.row_index,
                    email_addr,
                    FetchResult("auth_error", detail="Invalid email format."),
                )
            return

        def token_provider(acct: Account) -> Optional[str]:
            if refresh_token:
                tok, _err = get_access_token_from_refresh_token(refresh_token, client_id)
                if tok:
                    return tok
            if self.msal_token_func is not None:
                try:
                    return self.msal_token_func(acct)
                except Exception:
                    pass
            return None

        account = Account(
            email=email_addr,
            password=password,
            provider="microsoft",
        )

        try:
            result = fetch_latest_code(
                account,
                max_messages=0,
                max_age_hours=self.max_age_hours,
                timeout=20,
                access_token_provider=token_provider,
            )
        except Exception as e:
            result = FetchResult(
                status="connection_error",
                detail=f"Connection error: {str(e)}",
            )

        if not self.cancel_event.is_set():
            self.signals.completed.emit(self.run_id, self.row_index, email_addr, result)


class DeleteAllMailTask(QRunnable):
    def __init__(
        self,
        run_id: int,
        account: Account,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self.account = account
        self.cancel_event = cancel_event
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        if self.cancel_event.is_set():
            return
        try:
            result = delete_all_yandex_mail(self.account, timeout=18)
        except Exception:
            result = DeleteMailResult(
                status="connection_error",
                detail="Unexpected Yandex mail error. No password was logged.",
            )
        if not self.cancel_event.is_set():
            self.signals.completed.emit(self.run_id, self.account.email, result)


class OAuthSignals(QObject):
    completed = Signal(object)


class OAuthTask(QRunnable):
    def __init__(
        self,
        manager: MicrosoftOAuthManager,
        flow: dict,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.flow = flow
        self.cancel_event = cancel_event
        self.signals = OAuthSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.manager.complete_device_flow(self.flow, self.cancel_event)
        except OAuthError as exc:
            result = {"success": False, "email": "", "detail": str(exc)}
        except Exception:
            result = {"success": False, "email": "", "detail": "Microsoft sign-in failed safely."}
        self.signals.completed.emit(result)


class UpdateWorkerSignals(QObject):
    found = Signal(object)
    not_found = Signal()
    failed = Signal(str)


class UpdateCheckTask(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.signals = UpdateWorkerSignals()

    def run(self) -> None:
        try:
            update = check_for_updates(current_version=APP_VERSION)
            if update is not None:
                self.signals.found.emit(update)
            else:
                self.signals.not_found.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class DownloadWorkerSignals(QObject):
    progress = Signal(int, int, float)  # downloaded, total, percent
    completed = Signal(object)          # target_path
    failed = Signal(str)


class DownloadTask(QRunnable):
    def __init__(self, download_url: str, target_path: Path) -> None:
        super().__init__()
        self.download_url = download_url
        self.target_path = target_path
        self.signals = DownloadWorkerSignals()

    def run(self) -> None:
        def on_prog(downloaded: int, total: int, pct: float) -> None:
            self.signals.progress.emit(downloaded, total, pct)

        success = download_update_archive(
            self.download_url,
            self.target_path,
            progress_callback=on_prog,
        )
        if success:
            self.signals.completed.emit(self.target_path)
        else:
            self.signals.failed.emit("Download failed or connection timed out.")


class UpdateNotificationDialog(QDialog):
    def __init__(self, update_info: UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Theara Update Available")
        self.setFixedWidth(460)
        self.update_info = update_info
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(22, 20, 22, 20)

        header = QLabel(f"🚀 New Update Available: v{update_info.version}")
        header.setStyleSheet("color: #f5c542; font-size: 16px; font-weight: bold;")
        layout.addWidget(header)

        ver_lbl = QLabel(f"Current version: v{APP_VERSION}   ➜   Latest version: v{update_info.version}")
        ver_lbl.setStyleSheet("color: #a09d94; font-size: 12px;")
        layout.addWidget(ver_lbl)

        if update_info.changelog:
            cl_title = QLabel("What's new:")
            cl_title.setStyleSheet("color: #d7bf79; font-weight: bold; font-size: 12px;")
            layout.addWidget(cl_title)

            cl_box = QPlainTextEdit(update_info.changelog)
            cl_box.setReadOnly(True)
            cl_box.setFixedHeight(120)
            layout.addWidget(cl_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        later_btn = _button("Later")
        later_btn.clicked.connect(self.reject)
        btn_row.addWidget(later_btn)

        update_btn = _button("⚡ Update Now", "primary")
        update_btn.clicked.connect(self.accept)
        btn_row.addWidget(update_btn)

        layout.addLayout(btn_row)


class DownloadProgressDialog(QDialog):
    def __init__(self, update_info: UpdateInfo, thread_pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Updating to v{update_info.version}...")
        self.setFixedWidth(420)
        self.update_info = update_info
        self.thread_pool = thread_pool
        self._target_zip = Path(tempfile.gettempdir()) / f"Theara_update_v{update_info.version}.zip"

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self.title_label = QLabel(f"Downloading update (v{update_info.version})...")
        self.title_label.setStyleSheet("color: #fff8df; font-size: 14px; font-weight: bold;")
        layout.addWidget(self.title_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Connecting to server...")
        self.status_label.setStyleSheet("color: #99978f; font-size: 11px;")
        layout.addWidget(self.status_label)

        self.cancel_button = _button("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_button)
        layout.addLayout(btn_row)

        self._start_download()

    def _start_download(self) -> None:
        self._download_task = DownloadTask(self.update_info.download_url, self._target_zip)
        self._download_task.signals.progress.connect(self._on_progress)
        self._download_task.signals.completed.connect(self._on_completed)
        self._download_task.signals.failed.connect(self._on_failed)
        self.thread_pool.start(self._download_task)

    @Slot(int, int, float)
    def _on_progress(self, downloaded: int, total: int, pct: float) -> None:
        self.progress_bar.setValue(int(pct))
        if total > 0:
            mb_down = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self.status_label.setText(f"{mb_down:.1f} MB / {mb_total:.1f} MB ({pct:.0f}%)")
        else:
            mb_down = downloaded / (1024 * 1024)
            self.status_label.setText(f"{mb_down:.1f} MB downloaded...")

    @Slot(object)
    def _on_completed(self, zip_path: Path) -> None:
        self.status_label.setText("Applying update and restarting Theara...")
        self.cancel_button.setEnabled(False)
        self.title_label.setText("Restarting application...")
        QTimer.singleShot(500, lambda: self._apply_and_exit(zip_path))

    def _apply_and_exit(self, zip_path: Path) -> None:
        success = apply_update_and_restart(zip_path, executable_name="Theara.exe")
        if success:
            QApplication.quit()
        else:
            QMessageBox.critical(
                self,
                "Update Failed",
                "Could not launch the update installer. Please try again or download manually.",
            )
            self.reject()

    @Slot(str)
    def _on_failed(self, error: str) -> None:
        self.status_label.setText(f"Error: {error}")
        QMessageBox.warning(self, "Download Error", f"Failed to download the update:\n{error}")
        self.reject()


class MetricCard(QFrame):
    def __init__(self, caption: str, value: str = "0") -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(1)
        self.value = QLabel(value)
        self.value.setObjectName("cardValue")
        label = QLabel(caption)
        label.setObjectName("cardCaption")
        layout.addWidget(self.value)
        layout.addWidget(label)


class MainWindow(QMainWindow):
    VIEW_YANDEX = "yandex"
    VIEW_MICROSOFT = "microsoft"
    VIEW_2FA = "2fa"

    COL_CHECK = 0
    COL_EMAIL = 1
    COL_PROVIDER = 2
    COL_PASSWORD = 3
    COL_SENDER = 4
    COL_SUBJECT = 5
    COL_RECEIVED = 6
    COL_STATUS = 7
    COLUMN_DEFAULT_WIDTHS = (36, 210, 105, 95, 190, 335, 190, 130)
    COLUMN_MIN_WIDTHS = (36, 125, 75, 70, 105, 150, 125, 90)

    MAIL_COL_SUBMAIL = 0
    MAIL_COL_FACEBOOK_NAME = 1
    MAIL_COL_CODE = 2
    MAIL_COL_SUBJECT = 3
    MAIL_COL_RECEIVED = 4
    # Compatibility names used by row metadata and older integrations.
    MAIL_COL_ACCOUNT = MAIL_COL_SUBMAIL
    MAIL_COL_SENDER = MAIL_COL_FACEBOOK_NAME
    MAIL_COLUMN_DEFAULT_WIDTHS = (220, 180, 90, 330, 185)
    MAIL_COLUMN_MIN_WIDTHS = (135, 110, 65, 150, 125)
    MAIL_ACCOUNT_KEY_ROLE = int(Qt.ItemDataRole.UserRole)
    MAIL_MESSAGE_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 1
    MAIL_SEARCH_TEXT_ROLE = int(Qt.ItemDataRole.UserRole) + 2

    def __init__(self) -> None:
        super().__init__()
        configure_application_fonts()
        self.setWindowTitle("Theara - Mail & 2FA Code Tool")
        self.resize(1320, 750)
        self.setMinimumSize(920, 560)

        self.accounts: list[Account] = []
        self.results: dict[str, FetchResult] = {}
        self._current_view = self.VIEW_YANDEX
        self._mail_view = self.VIEW_YANDEX
        self._totp_active_secret = ""
        self._twofa_items: list[dict[str, str]] = []
        self.settings = load_settings(SETTINGS_FILE)
        self.microsoft_oauth: MicrosoftOAuthManager | None = None
        self._oauth_dialog: DeviceCodeDialog | None = None
        self._oauth_cancel_event = threading.Event()
        self._oauth_target: Account | None = None
        self._configure_oauth_manager()
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(4)
        self._run_id = 0
        self._cancel_event = threading.Event()
        self._pending: set[str] = set()
        self._active = False
        self._active_action = ""
        self._run_total = 0
        self._run_completed = 0
        self._delete_total_deleted = 0
        self._delete_total_folders = 0
        self._delete_successes = 0
        self._delete_partials = 0
        self._delete_unknowns = 0
        self._recent_columns_initialized = False
        self._hotmail_run_id = 0
        self._hotmail_cancel_event = threading.Event()
        self._hotmail_results: dict[int, FetchResult] = {}
        self._hotmail_active = False
        self._hotmail_total = 0
        self._hotmail_completed = 0
        self._hotmail_codes_found = 0
        self._build_ui()
        self._load_saved_accounts()
        QTimer.singleShot(2500, lambda: self._check_for_updates_interactive(manual=False))

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 13, 20, 13)
        mark = QLabel("T")
        mark.setObjectName("appMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(40, 40)
        header_layout.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel("Theara")
        title.setObjectName("title")
        version_badge = QLabel(f"v{APP_VERSION}")
        version_badge.setStyleSheet(
            "color: #101112; background-color: #d7bf79; "
            "font-size: 10px; font-weight: bold; border-radius: 4px; padding: 2px 6px;"
        )
        title_row.addWidget(title)
        title_row.addWidget(version_badge)
        title_row.addStretch(1)
        subtitle = QLabel(
            "Yandex | Zoho | Outlook | Hotmail | Local 2FA codes - one secure workspace"
        )
        subtitle.setObjectName("subtitle")
        names.addLayout(title_row)
        names.addWidget(subtitle)
        header_layout.addLayout(names)
        header_layout.addStretch(1)

        self.update_check_button = _button("Check update")
        self.update_check_button.setToolTip("Check for live application updates")
        self.update_check_button.clicked.connect(lambda: self._check_for_updates_interactive(manual=True))
        header_layout.addWidget(self.update_check_button)
        header_layout.addSpacing(10)

        secure = QLabel("●  This device | Windows encrypted storage")
        secure.setStyleSheet("color: #8ed6a2;")
        secure.setToolTip(
            "Accounts are saved only for this Windows user on this device. "
            "They do not sync to another computer."
        )
        header_layout.addWidget(secure)
        outer.addWidget(header)

        mode_nav = QFrame()
        mode_nav.setObjectName("modeNav")
        mode_nav_layout = QHBoxLayout(mode_nav)
        mode_nav_layout.setContentsMargins(18, 6, 18, 6)
        mode_nav_layout.setSpacing(7)
        mode_label = QLabel("Workspace")
        mode_label.setObjectName("muted")
        mode_nav_layout.addWidget(mode_label)

        self.mode_button_group = QButtonGroup(self)
        self.mode_button_group.setExclusive(True)
        self.yandex_view_button = _button("Yandex Mail", "modeSwitch")
        self.microsoft_view_button = _button("Hotmail / Outlook", "modeSwitch")
        self.twofa_view_button = _button("Get 2FA Code", "modeSwitch")
        self.view_buttons = {
            self.VIEW_YANDEX: self.yandex_view_button,
            self.VIEW_MICROSOFT: self.microsoft_view_button,
            self.VIEW_2FA: self.twofa_view_button,
        }
        for view, button in self.view_buttons.items():
            button.setCheckable(True)
            self.mode_button_group.addButton(button)
            button.clicked.connect(
                lambda checked, selected_view=view: (
                    self._switch_view(selected_view) if checked else None
                )
            )
            mode_nav_layout.addWidget(button)
        self.yandex_view_button.setChecked(True)
        mode_nav_layout.addStretch(1)
        self.mode_description = QLabel("Yandex and app-password mailboxes")
        self.mode_description.setObjectName("muted")
        self.mode_description.setAlignment(Qt.AlignmentFlag.AlignRight)
        mode_nav_layout.addWidget(self.mode_description)
        outer.addWidget(mode_nav)

        self.page_stack = QStackedWidget()
        self.mail_page = QWidget()
        self.mail_page.setObjectName("workspacePage")
        body = QVBoxLayout(self.mail_page)
        body.setContentsMargins(18, 15, 18, 14)
        body.setSpacing(11)

        self.yandex_container = QWidget()
        yandex_layout = QVBoxLayout(self.yandex_container)
        yandex_layout.setContentsMargins(0, 0, 0, 0)
        yandex_layout.setSpacing(11)

        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        self.account_card = MetricCard("ACCOUNTS")
        self.code_card = MetricCard("CODES FOUND")
        self.checked_card = MetricCard("CHECKED", "—")
        metrics.addWidget(self.account_card)
        metrics.addWidget(self.code_card)
        metrics.addWidget(self.checked_card)
        metrics.addStretch(1)
        yandex_layout.addLayout(metrics)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(7)
        self.add_button = _button("＋ Add")
        self.paste_button = _button("Paste list")
        self.import_button = _button("Import file")
        self.export_button = _button("Export file")
        self.remove_button = _button("Remove", "danger")
        self.alias_button = _button("Edit alias")
        self.delete_all_yandex_button = _button("Delete all Yandex mail", "danger")
        self.delete_all_yandex_button.setToolTip(
            "Permanently delete every message in every selectable folder of the "
            "selected Yandex mailboxes. Folder structure is kept."
        )
        self.microsoft_button = _button("Connect Microsoft")
        self.fetch_selected_button = _button("Fetch selected")
        self.fetch_all_button = _button("↻  Fetch all", "primary")
        self.stop_button = _button("Stop", "danger")
        self.stop_button.setEnabled(False)

        for widget in (
            self.add_button,
            self.paste_button,
            self.import_button,
            self.export_button,
            self.remove_button,
            self.alias_button,
            self.delete_all_yandex_button,
            self.microsoft_button,
        ):
            toolbar.addWidget(widget)
        toolbar.addSpacing(8)
        self.select_all = QCheckBox("Select all")
        self.select_all.setChecked(True)
        toolbar.addWidget(self.select_all)
        toolbar.addStretch(1)
        age_label = QLabel("Search")
        age_label.setObjectName("muted")
        toolbar.addWidget(age_label)
        self.age_combo = QComboBox()
        self.age_combo.addItem("last 1 hour", 1)
        self.age_combo.addItem("last 24 hours", 24)
        self.age_combo.addItem("last 3 days", 72)
        self.age_combo.addItem("last 7 days", 168)
        self.age_combo.setCurrentIndex(0)
        toolbar.addWidget(self.age_combo)
        toolbar.addWidget(self.fetch_selected_button)
        toolbar.addWidget(self.fetch_all_button)
        toolbar.addWidget(self.stop_button)
        yandex_layout.addLayout(toolbar)

        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setPlaceholderText(
            "Filter both tables: accounts, senders, subjects, or codes…"
        )
        filter_row.addWidget(self.filter_edit, 1)
        self.columns_out_button = _button("Columns -")
        self.columns_fit_button = _button("Fit columns")
        self.columns_in_button = _button("Columns +")
        self.columns_out_button.setToolTip("Shrink column widths (Ctrl+-)")
        self.columns_fit_button.setToolTip("Fit columns to the window (Ctrl+0)")
        self.columns_in_button.setToolTip("Expand column widths (Ctrl++)")
        filter_row.addWidget(self.columns_out_button)
        filter_row.addWidget(self.columns_fit_button)
        filter_row.addWidget(self.columns_in_button)
        self.copy_button = _button("Copy selected code")
        filter_row.addWidget(self.copy_button)
        yandex_layout.addLayout(filter_row)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "",
                "ACCOUNT",
                "PROVIDER",
                "SECRET",
                "SENDER",
                "SUBJECT",
                "RECEIVED",
                "STATUS",
            ]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(39)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.Fixed)
        header_view.setMinimumSectionSize(36)
        header_view.setStretchLastSection(False)
        for column in range(1, self.table.columnCount()):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate(self.COLUMN_DEFAULT_WIDTHS):
            header_view.resizeSection(column, width)
        self.content_splitter = QSplitter(Qt.Orientation.Vertical)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(self.table)

        self.recent_mail_panel = QFrame()
        self.recent_mail_panel.setObjectName("card")
        recent_layout = QVBoxLayout(self.recent_mail_panel)
        recent_layout.setContentsMargins(10, 8, 10, 10)
        recent_layout.setSpacing(7)
        self.recent_mail_title = QLabel("All Mail")
        recent_title_font = QFont(self.recent_mail_title.font())
        recent_title_font.setBold(True)
        recent_title_font.setPointSize(11)
        self.recent_mail_title.setFont(recent_title_font)
        recent_header = QHBoxLayout()
        recent_header.setContentsMargins(0, 0, 0, 0)
        recent_header.setSpacing(7)
        recent_header.addWidget(self.recent_mail_title)
        self.codes_first_checkbox = QCheckBox("Codes first")
        self.codes_first_checkbox.setChecked(True)
        self.codes_first_checkbox.setToolTip(
            "Keep messages with a verification code above other mail."
        )
        recent_header.addWidget(self.codes_first_checkbox)
        recent_header.addStretch(1)
        recent_find_label = QLabel("Find:")
        recent_find_label.setObjectName("muted")
        recent_header.addWidget(recent_find_label)
        self.recent_filter_edit = QLineEdit()
        self.recent_filter_edit.setClearButtonEnabled(True)
        self.recent_filter_edit.setPlaceholderText(
            "Facebook name or Yandex submail…"
        )
        self.recent_filter_edit.setMinimumWidth(260)
        self.recent_filter_edit.setMaximumWidth(430)
        self.recent_filter_edit.setToolTip(
            "Find All Mail rows by Facebook name or Yandex submail (Ctrl+F)."
        )
        recent_header.addWidget(self.recent_filter_edit)
        self.recent_match_label = QLabel("0 mail")
        self.recent_match_label.setObjectName("muted")
        self.recent_match_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.recent_match_label.setMinimumWidth(70)
        recent_header.addWidget(self.recent_match_label)
        recent_layout.addLayout(recent_header)

        self.recent_empty_label = QLabel("No mail fetched yet. Click Fetch all to load the last hour.")
        self.recent_empty_label.setObjectName("muted")
        self.recent_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recent_empty_label.setWordWrap(True)
        recent_layout.addWidget(self.recent_empty_label)

        self.recent_mail_table = QTableWidget(0, 5)
        self.recent_mail_table.setHorizontalHeaderLabels(
            ["YANDEX SUBMAIL", "FACEBOOK NAME", "CODE MAIL", "SUBJECT", "RECEIVED"]
        )
        self.recent_mail_table.setAlternatingRowColors(True)
        self.recent_mail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.recent_mail_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.recent_mail_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recent_mail_table.verticalHeader().setVisible(False)
        self.recent_mail_table.verticalHeader().setDefaultSectionSize(39)
        self.recent_mail_table.setMinimumHeight(120)
        mail_header = self.recent_mail_table.horizontalHeader()
        mail_header.setStretchLastSection(False)
        mail_header.setMinimumSectionSize(36)
        for column in range(self.recent_mail_table.columnCount()):
            mail_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate(self.MAIL_COLUMN_DEFAULT_WIDTHS):
            mail_header.resizeSection(column, width)
        recent_layout.addWidget(self.recent_mail_table, 1)

        self.content_splitter.addWidget(self.recent_mail_panel)
        self.content_splitter.setStretchFactor(0, 3)
        self.content_splitter.setStretchFactor(1, 2)
        self.content_splitter.setSizes([390, 230])
        yandex_layout.addWidget(self.content_splitter, 1)
        body.addWidget(self.yandex_container, 1)

        # ── Hotmail Container (Image 1 layout) ──
        self.hotmail_container = self._build_hotmail_page()
        body.addWidget(self.hotmail_container, 1)
        self.hotmail_container.hide()

        self.page_stack.addWidget(self.mail_page)
        outer.addWidget(self.page_stack, 1)

        footer = QFrame()
        footer.setObjectName("appFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 6, 18, 7)
        footer_layout.setSpacing(16)
        hint = QLabel(
            "Tip: double-click an account row for its newest code message, or an All Mail row for "
            "that exact body. Drag headers or use Columns - / Fit / +."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        footer_layout.addWidget(hint, 1)
        credits = QVBoxLayout()
        credits.setContentsMargins(0, 0, 0, 0)
        credits.setSpacing(0)
        self.created_by_label = QLabel("Created by Theara")
        self.created_by_label.setObjectName("footerCredit")
        self.created_by_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.copyright_label = QLabel("Copyright © 2026 · All Rights Reserved")
        self.copyright_label.setObjectName("footerCopyright")
        self.copyright_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        credits.addWidget(self.created_by_label)
        credits.addWidget(self.copyright_label)
        footer_layout.addLayout(credits)
        self.twofa_page = self._build_twofa_page()
        self.page_stack.addWidget(self.twofa_page)
        outer.addWidget(footer)

        self.statusBar().showMessage("Ready")
        self.add_button.clicked.connect(self._add_one)
        self.paste_button.clicked.connect(self._paste_accounts)
        self.import_button.clicked.connect(self._import_file)
        self.export_button.clicked.connect(self._export_file)
        self.remove_button.clicked.connect(self._remove_selected)
        self.alias_button.clicked.connect(self._edit_selected_alias)
        self.delete_all_yandex_button.clicked.connect(
            self._delete_all_yandex_mail_for_selection
        )
        self.microsoft_button.clicked.connect(self._connect_microsoft)
        self.fetch_selected_button.clicked.connect(self._fetch_selected)
        self.fetch_all_button.clicked.connect(self._fetch_all)
        self.stop_button.clicked.connect(self._stop_fetch)
        self.select_all.toggled.connect(self._set_all_checked)
        self.filter_edit.textChanged.connect(self._filter_rows)
        self.recent_filter_edit.textChanged.connect(
            lambda _text: self._filter_recent_mail_rows()
        )
        self.codes_first_checkbox.toggled.connect(
            lambda _checked: self._rebuild_recent_mail()
        )
        self.copy_button.clicked.connect(self._copy_selected_code)
        self.columns_out_button.clicked.connect(lambda: self._zoom_columns(0.86))
        self.columns_fit_button.clicked.connect(self._fit_columns)
        self.columns_in_button.clicked.connect(lambda: self._zoom_columns(1.16))
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.currentCellChanged.connect(self._account_selection_changed)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.recent_mail_table.cellClicked.connect(self._copy_recent_mail_code)
        self.recent_mail_table.cellDoubleClicked.connect(self._recent_mail_double_clicked)
        self._column_shortcuts = [
            QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self._zoom_columns(1.16)),
            QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self._zoom_columns(1.16)),
            QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self._zoom_columns(0.86)),
            QShortcut(QKeySequence("Ctrl+0"), self, activated=self._fit_columns),
            QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_recent_filter),
        ]
        self._switch_view(self.VIEW_YANDEX, announce=False)
        QTimer.singleShot(0, self._fit_columns)

    def _build_hotmail_page(self) -> QWidget:
        """Build the dedicated Hotmail/Outlook OAuth token and code extraction workspace."""
        page = QWidget()
        page.setObjectName("workspacePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 15, 18, 14)
        layout.setSpacing(12)

        # ── Top Card: Accounts Input & Actions (Matching Image 1) ──
        top_card = QFrame()
        top_card.setObjectName("sectionCard")
        top_layout = QVBoxLayout(top_card)
        top_layout.setContentsMargins(16, 14, 16, 14)
        top_layout.setSpacing(10)

        # Badge and header row
        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        self.hotmail_accounts_badge = QLabel("Accounts: 0/1000")
        self.hotmail_accounts_badge.setObjectName("accountsPill")
        header_row.addWidget(self.hotmail_accounts_badge)

        input_hint = QLabel("Format: email|pass|refresh_token|client_id  (or email|pass|refresh_token)")
        input_hint.setObjectName("muted")
        header_row.addWidget(input_hint)
        header_row.addStretch(1)

        age_label = QLabel("Search:")
        age_label.setObjectName("muted")
        header_row.addWidget(age_label)
        self.hotmail_age_combo = QComboBox()
        self.hotmail_age_combo.addItem("last 1 hour", 1)
        self.hotmail_age_combo.addItem("last 24 hours", 24)
        self.hotmail_age_combo.addItem("last 3 days", 72)
        self.hotmail_age_combo.addItem("all mail", 0)
        header_row.addWidget(self.hotmail_age_combo)
        top_layout.addLayout(header_row)

        # Multi-line Text Area
        self.hotmail_text_edit = QPlainTextEdit()
        self.hotmail_text_edit.setObjectName("codeEditor")
        self.hotmail_text_edit.setPlaceholderText("email|pass|refresh_token|client_id")
        self.hotmail_text_edit.setMinimumHeight(130)
        self.hotmail_text_edit.textChanged.connect(self._on_hotmail_text_changed)
        top_layout.addWidget(self.hotmail_text_edit)

        # Start button (Big Cyan Button from Image 1) and utility buttons
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.hotmail_start_button = QPushButton("Start")
        self.hotmail_start_button.setObjectName("startCyan")
        self.hotmail_start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hotmail_start_button.clicked.connect(self._start_hotmail_fetch)
        action_row.addWidget(self.hotmail_start_button, 1)

        self.hotmail_stop_button = _button("Stop", "danger")
        self.hotmail_stop_button.setEnabled(False)
        self.hotmail_stop_button.clicked.connect(self._stop_hotmail_fetch)
        action_row.addWidget(self.hotmail_stop_button)

        self.hotmail_clear_button = _button("Clear")
        self.hotmail_clear_button.clicked.connect(self._clear_hotmail_input)
        action_row.addWidget(self.hotmail_clear_button)

        self.hotmail_import_button = _button("Import .txt")
        self.hotmail_import_button.clicked.connect(self._import_hotmail_file)
        action_row.addWidget(self.hotmail_import_button)

        self.hotmail_export_button = _button("Export results")
        self.hotmail_export_button.clicked.connect(self._export_hotmail_results)
        action_row.addWidget(self.hotmail_export_button)

        top_layout.addLayout(action_row)
        layout.addWidget(top_card)

        # ── Bottom Card: Check Results (Matching Image 1) ──
        results_card = QFrame()
        results_card.setObjectName("sectionCard")
        results_layout = QVBoxLayout(results_card)
        results_layout.setContentsMargins(16, 14, 16, 14)
        results_layout.setSpacing(10)

        # Results header
        results_header = QHBoxLayout()
        results_header.setSpacing(10)
        results_title = QLabel("Check Results")
        results_title.setObjectName("sectionTitle")
        results_header.addWidget(results_title)

        self.hotmail_results_stats = QLabel("Total: 0 | Codes: 0 | Checked: 0")
        self.hotmail_results_stats.setObjectName("muted")
        results_header.addWidget(self.hotmail_results_stats)
        results_header.addStretch(1)

        self.hotmail_copy_all_codes_button = _button("Copy all codes")
        self.hotmail_copy_all_codes_button.clicked.connect(self._copy_all_hotmail_codes)
        results_header.addWidget(self.hotmail_copy_all_codes_button)

        self.hotmail_filter_edit = QLineEdit()
        self.hotmail_filter_edit.setPlaceholderText("Filter results (email, code, sender, subject)...")
        self.hotmail_filter_edit.setMinimumWidth(260)
        self.hotmail_filter_edit.textChanged.connect(self._filter_hotmail_results)
        results_header.addWidget(self.hotmail_filter_edit)
        results_layout.addLayout(results_header)

        # Results Table
        self.hotmail_results_table = QTableWidget(0, 8)
        self.hotmail_results_table.setHorizontalHeaderLabels([
            "#",
            "ACCOUNT / EMAIL",
            "STATUS",
            "CODE",
            "SUBJECT",
            "SENDER",
            "RECEIVED",
            "ACTION",
        ])
        self.hotmail_results_table.setAlternatingRowColors(True)
        self.hotmail_results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.hotmail_results_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.hotmail_results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hotmail_results_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.hotmail_results_table.verticalHeader().setVisible(False)
        self.hotmail_results_table.verticalHeader().setDefaultSectionSize(38)
        self.hotmail_results_table.horizontalHeader().setStretchLastSection(False)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.hotmail_results_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        self.hotmail_results_table.cellDoubleClicked.connect(self._on_hotmail_table_double_clicked)
        self.hotmail_results_table.customContextMenuRequested.connect(self._show_hotmail_context_menu)
        results_layout.addWidget(self.hotmail_results_table, 1)

        layout.addWidget(results_card, 1)
        return page

    def _on_hotmail_text_changed(self) -> None:
        raw_text = self.hotmail_text_edit.toPlainText()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        self.hotmail_accounts_badge.setText(f"Accounts: {len(lines)}/1000")

    def _start_hotmail_fetch(self) -> None:
        if self._hotmail_active:
            return
        raw_text = self.hotmail_text_edit.toPlainText()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not lines:
            QMessageBox.information(
                self,
                "No Accounts",
                "Please enter at least one Hotmail/Outlook account in the box.\nFormat: email|pass|refresh_token|client_id",
            )
            return

        self._hotmail_run_id += 1
        self._hotmail_cancel_event = threading.Event()
        self._hotmail_active = True
        self._hotmail_total = len(lines)
        self._hotmail_completed = 0
        self._hotmail_codes_found = 0
        self._hotmail_results.clear()

        self.hotmail_start_button.setEnabled(False)
        self.hotmail_stop_button.setEnabled(True)
        self.hotmail_clear_button.setEnabled(False)
        self.hotmail_import_button.setEnabled(False)

        self.hotmail_results_table.setRowCount(len(lines))
        self.hotmail_results_stats.setText(f"Total: {self._hotmail_total} | Codes: 0 | Checked: 0/{self._hotmail_total}")

        max_age_hours = int(self.hotmail_age_combo.currentData())

        for idx, line in enumerate(lines):
            parts = [p.strip() for p in line.split("|")]
            email_addr = parts[0] if parts else f"Account #{idx+1}"

            self._set_hotmail_table_row(
                idx,
                row_number=idx + 1,
                email=email_addr,
                status="Waiting...",
                status_color="#94A3B8",
                code="—",
                code_color="#94A3B8",
                subject="—",
                sender="—",
                received="—",
            )

            task = HotmailFetchTask(
                run_id=self._hotmail_run_id,
                row_index=idx,
                raw_line=line,
                max_age_hours=max_age_hours,
                cancel_event=self._hotmail_cancel_event,
                msal_token_func=self._token_for_account,
            )
            task.signals.completed.connect(self._on_hotmail_task_completed)
            self.thread_pool.start(task)

        self.statusBar().showMessage(f"Checking {len(lines)} Hotmail account(s)...")

    def _set_hotmail_table_row(
        self,
        row: int,
        row_number: int,
        email: str,
        status: str,
        status_color: str,
        code: str,
        code_color: str,
        subject: str,
        sender: str,
        received: str,
        detail: str = "",
    ) -> None:
        idx_item = QTableWidgetItem(str(row_number))
        idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        idx_item.setForeground(QColor("#64748B"))
        self.hotmail_results_table.setItem(row, 0, idx_item)

        email_item = QTableWidgetItem(email)
        email_item.setForeground(QColor("#F1F5F9"))
        self.hotmail_results_table.setItem(row, 1, email_item)

        status_item = QTableWidgetItem(status)
        status_item.setForeground(QColor(status_color))
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if detail:
            status_item.setToolTip(detail)
        self.hotmail_results_table.setItem(row, 2, status_item)

        code_item = QTableWidgetItem(code)
        code_font = QFont("Consolas", 11, QFont.Weight.Bold)
        code_item.setFont(code_font)
        code_item.setForeground(QColor(code_color))
        code_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if code and code != "—":
            code_item.setToolTip(f"Code: {code} (Double-click to copy)")
        self.hotmail_results_table.setItem(row, 3, code_item)

        subj_item = QTableWidgetItem(subject)
        subj_item.setForeground(QColor("#CBD5E1"))
        self.hotmail_results_table.setItem(row, 4, subj_item)

        sender_item = QTableWidgetItem(sender)
        sender_item.setForeground(QColor("#94A3B8"))
        self.hotmail_results_table.setItem(row, 5, sender_item)

        rec_item = QTableWidgetItem(received)
        rec_item.setForeground(QColor("#94A3B8"))
        self.hotmail_results_table.setItem(row, 6, rec_item)

        if code and code != "—":
            copy_btn = _button("Copy", "primary")
            copy_btn.setMaximumHeight(26)
            copy_btn.clicked.connect(lambda _, c=code: self._copy_single_hotmail_code(c))
            self.hotmail_results_table.setCellWidget(row, 7, copy_btn)
        else:
            action_item = QTableWidgetItem("—")
            action_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            action_item.setForeground(QColor("#64748B"))
            self.hotmail_results_table.setItem(row, 7, action_item)

    @Slot(int, int, str, object)
    def _on_hotmail_task_completed(self, run_id: int, row_index: int, email: str, result: FetchResult) -> None:
        if run_id != self._hotmail_run_id or not self._hotmail_active:
            return

        self._hotmail_results[row_index] = result
        self._hotmail_completed += 1

        status = getattr(result, "status", "connection_error")
        code = getattr(result, "code", "") or "—"
        subject = getattr(result, "subject", "") or "—"
        sender = getattr(result, "sender", "") or "—"
        received = getattr(result, "received", "") or "—"
        detail = getattr(result, "detail", "") or ""

        status_colors = {
            "found": "#10B981",
            "not_found": "#F59E0B",
            "auth_error": "#EF4444",
            "connection_error": "#EF4444",
            "oauth_required": "#38BDF8",
            "unsupported_provider": "#EF4444",
        }
        status_labels = {
            "found": "Code Found",
            "not_found": "No Recent Code",
            "auth_error": "Auth Failed",
            "connection_error": "Connection Error",
            "oauth_required": "Token Required",
            "unsupported_provider": "Invalid Provider",
        }

        status_label = status_labels.get(status, status.replace("_", " ").title())
        status_color = status_colors.get(status, "#94A3B8")
        code_color = "#00E5FF" if (code and code != "—") else "#94A3B8"

        if status == "found":
            self._hotmail_codes_found += 1

        self._set_hotmail_table_row(
            row_index,
            row_number=row_index + 1,
            email=email,
            status=status_label,
            status_color=status_color,
            code=code,
            code_color=code_color,
            subject=subject,
            sender=sender,
            received=received,
            detail=detail,
        )

        self.hotmail_results_stats.setText(
            f"Total: {self._hotmail_total} | Codes: {self._hotmail_codes_found} | Checked: {self._hotmail_completed}/{self._hotmail_total}"
        )

        if self._hotmail_completed >= self._hotmail_total:
            self._hotmail_active = False
            self.hotmail_start_button.setEnabled(True)
            self.hotmail_stop_button.setEnabled(False)
            self.hotmail_clear_button.setEnabled(True)
            self.hotmail_import_button.setEnabled(True)
            self.statusBar().showMessage(
                f"Hotmail check finished. {self._hotmail_codes_found} code(s) found across {self._hotmail_total} accounts.",
                8000,
            )

    def _stop_hotmail_fetch(self) -> None:
        if not self._hotmail_active:
            return
        self._hotmail_cancel_event.set()
        self._hotmail_active = False
        self.hotmail_start_button.setEnabled(True)
        self.hotmail_stop_button.setEnabled(False)
        self.hotmail_clear_button.setEnabled(True)
        self.hotmail_import_button.setEnabled(True)
        self.statusBar().showMessage("Hotmail check stopped by user.", 5000)

    def _clear_hotmail_input(self) -> None:
        self.hotmail_text_edit.clear()
        self.hotmail_results_table.setRowCount(0)
        self.hotmail_results_stats.setText("Total: 0 | Codes: 0 | Checked: 0")
        self.hotmail_accounts_badge.setText("Accounts: 0/1000")

    def _import_hotmail_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Hotmail Accounts",
            "",
            "Text Files (*.txt);;All Files (*.*)",
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
            self.hotmail_text_edit.setPlainText(content)
            self.statusBar().showMessage(f"Imported {path}", 4000)
        except Exception as e:
            QMessageBox.warning(self, "Import Error", f"Could not read file: {e}")

    def _export_hotmail_results(self) -> None:
        if self.hotmail_results_table.rowCount() == 0:
            QMessageBox.information(self, "Export Results", "No results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Results",
            "hotmail_results.txt",
            "Text Files (*.txt);;All Files (*.*)",
        )
        if not path:
            return
        try:
            lines = []
            for row in range(self.hotmail_results_table.rowCount()):
                email_item = self.hotmail_results_table.item(row, 1)
                status_item = self.hotmail_results_table.item(row, 2)
                code_item = self.hotmail_results_table.item(row, 3)
                subj_item = self.hotmail_results_table.item(row, 4)

                email = email_item.text() if email_item else ""
                status = status_item.text() if status_item else ""
                code = code_item.text() if code_item else ""
                subj = subj_item.text() if subj_item else ""
                lines.append(f"{email}|{code}|{status}|{subj}")

            Path(path).write_text("\n".join(lines), encoding="utf-8")
            self.statusBar().showMessage(f"Exported {len(lines)} results to {path}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "Export Error", f"Could not save file: {e}")

    def _copy_single_hotmail_code(self, code: str) -> None:
        if code and code != "—":
            QGuiApplication.clipboard().setText(code)
            self.statusBar().showMessage(f"Copied code {code} to clipboard.", 3000)

    def _copy_all_hotmail_codes(self) -> None:
        codes = []
        for row in range(self.hotmail_results_table.rowCount()):
            code_item = self.hotmail_results_table.item(row, 3)
            if code_item:
                c = code_item.text().strip()
                if c and c != "—":
                    codes.append(c)
        if not codes:
            self.statusBar().showMessage("No codes found to copy.", 3000)
            return
        QGuiApplication.clipboard().setText("\n".join(codes))
        self.statusBar().showMessage(f"Copied {len(codes)} code(s) to clipboard.", 4000)

    def _filter_hotmail_results(self, text: str) -> None:
        query = text.strip().casefold()
        for row in range(self.hotmail_results_table.rowCount()):
            if not query:
                self.hotmail_results_table.setRowHidden(row, False)
                continue
            match = False
            for col in range(self.hotmail_results_table.columnCount()):
                item = self.hotmail_results_table.item(row, col)
                if item and query in item.text().casefold():
                    match = True
                    break
            self.hotmail_results_table.setRowHidden(row, not match)

    def _on_hotmail_table_double_clicked(self, row: int, col: int) -> None:
        item = self.hotmail_results_table.item(row, col)
        if not item:
            return
        text = item.text().strip()
        if text and text != "—":
            QGuiApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"Copied '{text}' to clipboard.", 3000)

    def _show_hotmail_context_menu(self, pos: QPoint) -> None:
        item = self.hotmail_results_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        menu = QMenu(self)

        code_item = self.hotmail_results_table.item(row, 3)
        code = code_item.text() if code_item else ""
        if code and code != "—":
            act_code = menu.addAction(f"Copy Code ({code})")
            act_code.triggered.connect(lambda: self._copy_single_hotmail_code(code))

        email_item = self.hotmail_results_table.item(row, 1)
        email = email_item.text() if email_item else ""
        if email:
            act_email = menu.addAction(f"Copy Email ({email})")
            act_email.triggered.connect(lambda: QGuiApplication.clipboard().setText(email))

        menu.addSeparator()
        act_copy_all = menu.addAction("Copy All Found Codes")
        act_copy_all.triggered.connect(self._copy_all_hotmail_codes)

        act_export = menu.addAction("Export Results...")
        act_export.triggered.connect(self._export_hotmail_results)

        menu.exec(self.hotmail_results_table.viewport().mapToGlobal(pos))

    def _build_twofa_page(self) -> QWidget:
        """Build the dedicated 2FA authenticator workspace matching user screenshot."""
        page = QWidget()
        page.setObjectName("workspacePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 15, 18, 14)
        layout.setSpacing(12)

        # ── Top Card: Accounts: 0/1000, 2fa Key editor & Full-width Start button ──
        top_card = QFrame()
        top_card.setObjectName("sectionCard")
        top_layout = QVBoxLayout(top_card)
        top_layout.setContentsMargins(16, 14, 16, 14)
        top_layout.setSpacing(10)

        # Header Row: Accounts: 0/1000 badge
        header_row = QHBoxLayout()
        self.twofa_accounts_badge = QLabel("Accounts: 0/1000")
        self.twofa_accounts_badge.setObjectName("accountsPill")
        self.twofa_keys_badge = self.twofa_accounts_badge
        header_row.addWidget(self.twofa_accounts_badge)
        header_row.addStretch(1)
        top_layout.addLayout(header_row)

        # Zero-height totp_secret_edit for Ctrl+F and test compatibility
        self.totp_secret_edit = QLineEdit()
        self.totp_secret_edit.setMaximumHeight(0)
        self.totp_secret_edit.setStyleSheet("max-height: 0px; border: none; margin: 0; padding: 0;")
        top_layout.addWidget(self.totp_secret_edit)

        # Multi-line Text Area with "2fa Key" placeholder
        self.twofa_text_edit = QPlainTextEdit()
        self.twofa_text_edit.setObjectName("codeEditor")
        self.twofa_text_edit.setPlaceholderText("2fa Key")
        self.twofa_text_edit.setMinimumHeight(130)
        self.twofa_text_edit.textChanged.connect(self._on_twofa_text_changed)
        top_layout.addWidget(self.twofa_text_edit)

        # 100% Full-Width Start Button
        self.totp_generate_button = QPushButton("Start")
        self.totp_generate_button.setObjectName("startCyan")
        self.totp_generate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.totp_generate_button.clicked.connect(self._generate_totp_code)
        top_layout.addWidget(self.totp_generate_button)

        layout.addWidget(top_card)

        # ── Bottom Card: KEY 2FA ──
        results_card = QFrame()
        results_card.setObjectName("sectionCard")
        results_layout = QVBoxLayout(results_card)
        results_layout.setContentsMargins(16, 14, 16, 14)
        results_layout.setSpacing(10)

        results_title = QLabel("KEY 2FA")
        results_title.setObjectName("sectionTitle")
        results_title.setStyleSheet("font-weight: 800; font-size: 13px; color: #F8FAFC;")
        results_layout.addWidget(results_title)

        self.twofa_empty_label = QLabel("Input KEY 2FA.")
        self.twofa_empty_label.setObjectName("muted")
        self.twofa_empty_label.setStyleSheet("color: #94A3B8; font-size: 12px;")
        results_layout.addWidget(self.twofa_empty_label)

        # Active container for generated codes/table
        self.twofa_active_container = QWidget()
        active_vbox = QVBoxLayout(self.twofa_active_container)
        active_vbox.setContentsMargins(0, 4, 0, 0)
        active_vbox.setSpacing(10)

        # Live 2FA code banner
        code_banner = QFrame()
        code_banner.setObjectName("sectionCard")
        code_banner_layout = QHBoxLayout(code_banner)
        code_banner_layout.setContentsMargins(12, 6, 12, 6)
        code_banner_layout.setSpacing(16)

        code_vbox = QVBoxLayout()
        code_vbox.setSpacing(1)
        self.totp_code_label = QLabel("——————")
        self.totp_code_label.setObjectName("twoFactorCode")
        code_vbox.addWidget(self.totp_code_label)
        code_banner_layout.addLayout(code_vbox)

        timer_vbox = QVBoxLayout()
        timer_vbox.setSpacing(4)
        self.totp_countdown_label = QLabel("Generate a code to start the 30-second timer.")
        self.totp_countdown_label.setObjectName("muted")
        timer_vbox.addWidget(self.totp_countdown_label)
        self.totp_progress = QProgressBar()
        self.totp_progress.setRange(0, 30)
        self.totp_progress.setValue(0)
        self.totp_progress.setTextVisible(False)
        timer_vbox.addWidget(self.totp_progress)
        self.totp_error_label = QLabel("")
        self.totp_error_label.setObjectName("twoFactorError")
        self.totp_error_label.setVisible(False)
        timer_vbox.addWidget(self.totp_error_label)
        code_banner_layout.addLayout(timer_vbox, 1)

        self.totp_copy_button = _button("Copy 2FA code", "primary")
        self.totp_copy_button.setEnabled(False)
        self.totp_copy_button.clicked.connect(self._copy_totp_code)
        code_banner_layout.addWidget(self.totp_copy_button)
        active_vbox.addWidget(code_banner)

        # Results Table for viewing multiple keys
        self.twofa_results_table = QTableWidget(0, 6)
        self.twofa_results_table.setHorizontalHeaderLabels([
            "#",
            "ACCOUNT / LABEL",
            "2FA SECRET",
            "CURRENT 2FA CODE",
            "TIMER / EXPIRES",
            "ACTION",
        ])
        self.twofa_results_table.setAlternatingRowColors(True)
        self.twofa_results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.twofa_results_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.twofa_results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.twofa_results_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.twofa_results_table.verticalHeader().setVisible(False)
        self.twofa_results_table.verticalHeader().setDefaultSectionSize(34)
        self.twofa_results_table.horizontalHeader().setStretchLastSection(False)
        self.twofa_results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.twofa_results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.twofa_results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.twofa_results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.twofa_results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.twofa_results_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        self.twofa_results_table.cellClicked.connect(self._on_twofa_table_cell_clicked)
        self.twofa_results_table.cellDoubleClicked.connect(self._on_twofa_table_double_clicked)
        self.twofa_results_table.customContextMenuRequested.connect(self._show_twofa_context_menu)
        active_vbox.addWidget(self.twofa_results_table, 1)

        results_layout.addWidget(self.twofa_active_container, 1)
        self.twofa_active_container.setVisible(False)

        layout.addWidget(results_card, 1)

        # Hidden controls for compatibility
        self.totp_show_secret = QCheckBox("Show secret")
        self.totp_show_secret.setChecked(True)
        self.totp_show_secret.toggled.connect(self._on_twofa_show_secret_toggled)
        self.totp_clear_button = QPushButton("Clear")
        self.totp_clear_button.clicked.connect(self._clear_totp)
        self.twofa_copy_all_button = QPushButton("Copy all codes")
        self.twofa_copy_all_button.clicked.connect(self._copy_all_twofa_codes)
        self.twofa_results_stats = QLabel("Total: 0 | Codes: 0 | Active: 0")

        # Wire inputs & timer
        self.totp_secret_edit.returnPressed.connect(self._generate_totp_code)
        self.totp_secret_edit.textEdited.connect(self._totp_secret_was_edited)
        self.totp_timer = QTimer(self)
        self.totp_timer.setInterval(1000)
        self.totp_timer.timeout.connect(self._refresh_totp_code)
        self.totp_timer.start()
        return page

    def _switch_view(self, view: str, *, announce: bool = True) -> None:
        """Switch between provider mail workspaces and the local 2FA page."""
        if view not in self.view_buttons:
            return
        if self._active or self._hotmail_active:
            self.view_buttons[self._current_view].setChecked(True)
            if announce:
                self.statusBar().showMessage(
                    "Stop or finish the current task before switching workspaces.",
                    5000,
                )
            return

        self._current_view = view
        self.view_buttons[view].setChecked(True)
        if view == self.VIEW_2FA:
            self.page_stack.setCurrentWidget(self.twofa_page)
            self.mode_description.setText("Local code generator · secrets are not saved")
            if announce:
                self.statusBar().showMessage("2FA code generator ready.", 3500)
            return

        self._mail_view = view
        self.page_stack.setCurrentWidget(self.mail_page)
        self.yandex_container.setVisible(view == self.VIEW_YANDEX)
        self.hotmail_container.setVisible(view == self.VIEW_MICROSOFT)
        self.table.clearSelection()
        self.recent_mail_table.clearSelection()
        self._apply_mail_view_controls()
        self._filter_rows(self.filter_edit.text())
        self._update_metrics()
        if announce:
            label = "Yandex Mail" if view == self.VIEW_YANDEX else "Hotmail / Outlook"
            self.statusBar().showMessage(f"{label} workspace selected.", 3500)

    def _apply_mail_view_controls(self) -> None:
        """Show provider-appropriate actions without changing stored accounts."""
        microsoft_view = self._mail_view == self.VIEW_MICROSOFT
        self.alias_button.setVisible(not microsoft_view)
        self.delete_all_yandex_button.setVisible(not microsoft_view)
        self.paste_button.setVisible(not microsoft_view)
        self.import_button.setVisible(not microsoft_view)
        self.export_button.setVisible(not microsoft_view)
        self.microsoft_button.setVisible(microsoft_view)
        self.add_button.setText("＋ Add Microsoft" if microsoft_view else "＋ Add")
        self.fetch_all_button.setText(
            "↻  Fetch Microsoft" if microsoft_view else "↻  Fetch Yandex"
        )
        self.recent_mail_title.setText(
            "All Mail — Hotmail / Outlook" if microsoft_view else "All Mail"
        )
        self.recent_mail_table.horizontalHeaderItem(self.MAIL_COL_SUBMAIL).setText(
            "RECIPIENT" if microsoft_view else "YANDEX SUBMAIL"
        )
        self.recent_mail_table.horizontalHeaderItem(self.MAIL_COL_FACEBOOK_NAME).setText(
            "SENDER / NAME" if microsoft_view else "FACEBOOK NAME"
        )
        self.recent_filter_edit.setPlaceholderText(
            "Sender name or recipient…"
            if microsoft_view
            else "Facebook name or Yandex submail…"
        )
        self.mode_description.setText(
            "Microsoft OAuth mailboxes"
            if microsoft_view
            else "Yandex and app-password mailboxes"
        )

    @staticmethod
    def _resolved_provider(account: Account) -> str:
        return detect_provider(account.email) if account.provider == "auto" else account.provider

    def _account_matches_mail_view(self, account: Account) -> bool:
        resolved = self._resolved_provider(account)
        if self._mail_view == self.VIEW_MICROSOFT:
            return resolved == "microsoft"
        # Keep existing Zoho/custom password-based accounts accessible rather
        # than orphaning them when the requested navigation is enabled.
        return resolved != "microsoft"

    def _accounts_for_current_mail_view(self) -> list[Account]:
        return [account for account in self.accounts if self._account_matches_mail_view(account)]

    def _on_twofa_text_changed(self) -> None:
        raw_text = self.twofa_text_edit.toPlainText()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        self.twofa_accounts_badge.setText(f"Accounts: {len(lines)}/1000")
        if lines:
            first = lines[0]
            parts = [p.strip() for p in first.split("|") if p.strip()]
            first_secret = parts[-1] if len(parts) >= 2 else first
            self.totp_secret_edit.blockSignals(True)
            self.totp_secret_edit.setText(first_secret)
            self.totp_secret_edit.blockSignals(False)

            # Live update: automatically calculate & show 2FA code if key is valid Base32
            try:
                generate_totp(first_secret)
                self._generate_totp_code(announce=False)
            except InvalidTOTPSecret:
                pass
        else:
            self.totp_secret_edit.blockSignals(True)
            self.totp_secret_edit.clear()
            self.totp_secret_edit.blockSignals(False)
            self._clear_totp_display_only()

    def _clear_totp_display_only(self) -> None:
        self._totp_active_secret = ""
        self._twofa_items.clear()
        self.totp_code_label.setText("——————")
        self.totp_countdown_label.setText("Generate a code to start the 30-second timer.")
        self.totp_progress.setValue(0)
        self.totp_error_label.clear()
        self.totp_error_label.setVisible(False)
        self.totp_copy_button.setEnabled(False)
        self.twofa_results_table.setRowCount(0)
        self.twofa_results_stats.setText("Total: 0 | Codes: 0 | Active: 0")
        self.twofa_empty_label.setVisible(True)
        self.twofa_active_container.setVisible(False)

    def _on_twofa_show_secret_toggled(self, checked: bool) -> None:
        self.totp_secret_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._update_twofa_table_secrets_visibility(checked)

    def _update_twofa_table_secrets_visibility(self, visible: bool) -> None:
        for row in range(self.twofa_results_table.rowCount()):
            if row < len(self._twofa_items):
                secret = self._twofa_items[row]["secret"]
                display = secret if visible else "•" * min(len(secret), 16)
                item = self.twofa_results_table.item(row, 2)
                if item:
                    item.setText(display)
                    item.setToolTip(secret if visible else "")

    def _generate_totp_code(self, announce: bool = True) -> None:
        text_content = self.twofa_text_edit.toPlainText().strip()
        single_secret = self.totp_secret_edit.text().strip()

        if not text_content and single_secret:
            text_content = single_secret
            self.twofa_text_edit.blockSignals(True)
            self.twofa_text_edit.setPlainText(single_secret)
            self.twofa_text_edit.blockSignals(False)
        elif text_content and not single_secret:
            lines = [l.strip() for l in text_content.splitlines() if l.strip()]
            if lines:
                parts = [p.strip() for p in lines[0].split("|") if p.strip()]
                single_secret = parts[-1] if len(parts) >= 2 else lines[0]
                self.totp_secret_edit.blockSignals(True)
                self.totp_secret_edit.setText(single_secret)
                self.totp_secret_edit.blockSignals(False)

        primary_secret = single_secret
        if not primary_secret and text_content:
            lines = [l.strip() for l in text_content.splitlines() if l.strip()]
            if lines:
                parts = [p.strip() for p in lines[0].split("|") if p.strip()]
                primary_secret = parts[-1] if len(parts) >= 2 else lines[0]

        try:
            primary_code = generate_totp(primary_secret)
        except InvalidTOTPSecret:
            self._totp_active_secret = ""
            self.totp_code_label.setText("——————")
            self.totp_progress.setValue(0)
            self.totp_countdown_label.setText("Enter a valid Base32 2FA secret.")
            self.totp_error_label.setText(
                "The secret is empty or invalid. Check the authenticator setup key and try again."
            )
            self.totp_error_label.setVisible(True)
            self.totp_copy_button.setEnabled(False)
            self.twofa_results_table.setRowCount(0)
            self.twofa_results_stats.setText("Total: 0 | Codes: 0 | Active: 0")
            self.twofa_empty_label.setVisible(False)
            self.twofa_active_container.setVisible(True)
            return

        self._totp_active_secret = primary_secret
        self.totp_error_label.clear()
        self.totp_error_label.setVisible(False)
        self.totp_code_label.setText(primary_code)
        self.totp_copy_button.setEnabled(True)
        self.twofa_empty_label.setVisible(False)
        self.twofa_active_container.setVisible(True)

        lines = [l.strip() for l in text_content.splitlines() if l.strip()]
        if not lines and primary_secret:
            lines = [primary_secret]

        self._twofa_items = []
        self.twofa_results_table.setRowCount(len(lines))
        codes_count = 0
        remaining = totp_seconds_remaining()

        for idx, line in enumerate(lines):
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                label = parts[0]
                sec = parts[-1]
            else:
                label = f"Key #{idx + 1}"
                sec = line

            try:
                c = generate_totp(sec)
                codes_count += 1
            except InvalidTOTPSecret:
                c = "INVALID"

            self._twofa_items.append({"label": label, "secret": sec, "code": c})
            self._set_twofa_table_row(idx, idx + 1, label, sec, c, remaining)

        self.twofa_results_stats.setText(
            f"Total: {len(lines)} | Codes: {codes_count} | Active: {len(lines)}"
        )
        self.twofa_accounts_badge.setText(f"Accounts: {len(lines)}/1000")

        self._refresh_totp_code()
        if announce:
            self.statusBar().showMessage(f"Generated 2FA codes for {len(lines)} key(s) locally.", 3000)

    def _set_twofa_table_row(
        self,
        row: int,
        row_number: int,
        label: str,
        secret: str,
        code: str,
        remaining: int,
    ) -> None:
        idx_item = QTableWidgetItem(str(row_number))
        idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        idx_item.setForeground(QColor("#64748B"))
        self.twofa_results_table.setItem(row, 0, idx_item)

        label_item = QTableWidgetItem(label)
        label_item.setForeground(QColor("#F1F5F9"))
        self.twofa_results_table.setItem(row, 1, label_item)

        show = self.totp_show_secret.isChecked()
        display_sec = secret if show else "•" * min(len(secret), 16)
        secret_item = QTableWidgetItem(display_sec)
        secret_item.setFont(QFont("Consolas", 10))
        secret_item.setForeground(QColor("#94A3B8"))
        if show:
            secret_item.setToolTip(secret)
        self.twofa_results_table.setItem(row, 2, secret_item)

        code_item = QTableWidgetItem(code)
        code_font = QFont("Consolas", 12, QFont.Weight.Bold)
        code_item.setFont(code_font)
        code_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        code_item.setForeground(QColor("#00E5FF" if code != "INVALID" else "#EF4444"))
        if code != "INVALID":
            code_item.setToolTip(f"Code: {code} (Double-click to copy)")
        self.twofa_results_table.setItem(row, 3, code_item)

        timer_text = f"{remaining}s remaining" if code != "INVALID" else "Invalid key"
        timer_item = QTableWidgetItem(timer_text)
        timer_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        timer_item.setForeground(QColor("#38BDF8" if code != "INVALID" else "#EF4444"))
        self.twofa_results_table.setItem(row, 4, timer_item)

        if code != "INVALID":
            copy_btn = _button("Copy", "primary")
            copy_btn.setMaximumHeight(26)
            copy_btn.clicked.connect(lambda _, c=code: self._copy_single_twofa_code(c))
            self.twofa_results_table.setCellWidget(row, 5, copy_btn)
        else:
            dash_item = QTableWidgetItem("—")
            dash_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dash_item.setForeground(QColor("#64748B"))
            self.twofa_results_table.setItem(row, 5, dash_item)

    def _on_twofa_table_cell_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._twofa_items):
            item = self._twofa_items[row]
            sec = item["secret"]
            try:
                code = generate_totp(sec)
                self._totp_active_secret = sec
                self.totp_code_label.setText(code)
                self.totp_copy_button.setEnabled(True)
                self.totp_secret_edit.setText(sec)
                remaining = totp_seconds_remaining()
                self.totp_countdown_label.setText(f"New code in {remaining} second(s)")
                self.totp_progress.setValue(remaining)
                self.statusBar().showMessage(f"Selected {item['label']} ({code})", 2500)
            except InvalidTOTPSecret:
                pass

    def _totp_secret_was_edited(self, text: str) -> None:
        """Prevent an edited field from displaying a code for the old secret."""
        if not self.twofa_text_edit.toPlainText().strip() or "\n" not in self.twofa_text_edit.toPlainText().strip():
            self.twofa_text_edit.blockSignals(True)
            self.twofa_text_edit.setPlainText(text)
            self.twofa_text_edit.blockSignals(False)
        count = 1 if text.strip() else 0
        self.twofa_accounts_badge.setText(f"Accounts: {count}/1000")
        if not self._totp_active_secret:
            return
        self._totp_active_secret = ""
        self.totp_code_label.setText("——————")
        self.totp_countdown_label.setText("Generate a code for the updated secret.")
        self.totp_progress.setValue(0)
        self.totp_copy_button.setEnabled(False)

    def _refresh_totp_code(self) -> None:
        if not self._totp_active_secret:
            return
        try:
            code = generate_totp(self._totp_active_secret)
        except InvalidTOTPSecret:
            self._clear_totp()
            return
        remaining = totp_seconds_remaining()
        self.totp_code_label.setText(code)
        self.totp_countdown_label.setText(f"New code in {remaining} second(s)")
        self.totp_progress.setValue(remaining)

        # Also refresh active table rows
        for row in range(self.twofa_results_table.rowCount()):
            if row < len(self._twofa_items):
                item_data = self._twofa_items[row]
                sec = item_data["secret"]
                try:
                    c = generate_totp(sec)
                except InvalidTOTPSecret:
                    c = "INVALID"
                item_data["code"] = c

                code_item = self.twofa_results_table.item(row, 3)
                if code_item:
                    code_item.setText(c)

                timer_item = self.twofa_results_table.item(row, 4)
                if timer_item:
                    timer_item.setText(f"{remaining}s remaining" if c != "INVALID" else "Invalid key")

    def _copy_totp_code(self) -> None:
        code = self.totp_code_label.text()
        if not (len(code) == 6 and code.isdigit()):
            return
        QGuiApplication.clipboard().setText(code)
        self.statusBar().showMessage(f"Copied 2FA code {code}.", 3000)

    def _copy_single_twofa_code(self, code: str) -> None:
        if code and code != "INVALID" and code != "—":
            QGuiApplication.clipboard().setText(code)
            self.statusBar().showMessage(f"Copied 2FA code {code} to clipboard.", 3000)

    def _copy_all_twofa_codes(self) -> None:
        codes = []
        for row in range(self.twofa_results_table.rowCount()):
            code_item = self.twofa_results_table.item(row, 3)
            if code_item:
                c = code_item.text().strip()
                if c and c != "INVALID" and c != "—":
                    codes.append(c)
        if not codes:
            self.statusBar().showMessage("No valid 2FA codes found to copy.", 3000)
            return
        QGuiApplication.clipboard().setText("\n".join(codes))
        self.statusBar().showMessage(f"Copied {len(codes)} 2FA code(s) to clipboard.", 4000)

    def _filter_twofa_results(self, text: str) -> None:
        query = text.strip().casefold()
        for row in range(self.twofa_results_table.rowCount()):
            if not query:
                self.twofa_results_table.setRowHidden(row, False)
                continue
            match = False
            for col in range(self.twofa_results_table.columnCount()):
                item = self.twofa_results_table.item(row, col)
                if item and query in item.text().casefold():
                    match = True
                    break
            self.twofa_results_table.setRowHidden(row, not match)

    def _on_twofa_table_double_clicked(self, row: int, col: int) -> None:
        item = self.twofa_results_table.item(row, col)
        if not item:
            return
        text = item.text().strip()
        if text and text != "—" and text != "INVALID":
            QGuiApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"Copied '{text}' to clipboard.", 3000)

    def _show_twofa_context_menu(self, pos: QPoint) -> None:
        item = self.twofa_results_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        menu = QMenu(self)

        code_item = self.twofa_results_table.item(row, 3)
        code = code_item.text() if code_item else ""
        if code and code != "INVALID" and code != "—":
            act_code = menu.addAction(f"Copy Code ({code})")
            act_code.triggered.connect(lambda: self._copy_single_twofa_code(code))

        label_item = self.twofa_results_table.item(row, 1)
        label = label_item.text() if label_item else ""
        if label:
            act_label = menu.addAction(f"Copy Label ({label})")
            act_label.triggered.connect(lambda: QGuiApplication.clipboard().setText(label))

        if row < len(self._twofa_items):
            sec = self._twofa_items[row]["secret"]
            act_sec = menu.addAction("Copy 2FA Secret")
            act_sec.triggered.connect(lambda: QGuiApplication.clipboard().setText(sec))

        menu.addSeparator()
        act_copy_all = menu.addAction("Copy All Active Codes")
        act_copy_all.triggered.connect(self._copy_all_twofa_codes)

        act_export = menu.addAction("Export Results...")
        act_export.triggered.connect(self._export_twofa_results)

        menu.exec(self.twofa_results_table.viewport().mapToGlobal(pos))

    def _import_twofa_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import 2FA Keys",
            "",
            "Text Files (*.txt);;All Files (*.*)",
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
            self.twofa_text_edit.setPlainText(content)
            self.statusBar().showMessage(f"Imported {path}", 4000)
        except Exception as e:
            QMessageBox.warning(self, "Import Error", f"Could not read file: {e}")

    def _export_twofa_results(self) -> None:
        if self.twofa_results_table.rowCount() == 0:
            QMessageBox.information(self, "Export Results", "No results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export 2FA Results",
            "twofa_results.txt",
            "Text Files (*.txt);;All Files (*.*)",
        )
        if not path:
            return
        try:
            lines = []
            for row in range(self.twofa_results_table.rowCount()):
                label_item = self.twofa_results_table.item(row, 1)
                code_item = self.twofa_results_table.item(row, 3)
                label = label_item.text() if label_item else ""
                code = code_item.text() if code_item else ""
                sec = self._twofa_items[row]["secret"] if row < len(self._twofa_items) else ""
                lines.append(f"{label}|{sec}|{code}")

            Path(path).write_text("\n".join(lines), encoding="utf-8")
            self.statusBar().showMessage(f"Exported {len(lines)} 2FA results to {path}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "Export Error", f"Could not save file: {e}")

    def _clear_totp(self) -> None:
        self._totp_active_secret = ""
        self._twofa_items.clear()
        self.totp_secret_edit.clear()
        self.twofa_text_edit.clear()
        self.totp_show_secret.setChecked(True)
        self.totp_code_label.setText("——————")
        self.totp_countdown_label.setText("Generate a code to start the 30-second timer.")
        self.totp_progress.setValue(0)
        self.totp_error_label.clear()
        self.totp_error_label.setVisible(False)
        self.totp_copy_button.setEnabled(False)
        self.twofa_accounts_badge.setText("Accounts: 0/1000")
        self.twofa_results_table.setRowCount(0)
        self.twofa_results_stats.setText("Total: 0 | Codes: 0 | Active: 0")
        self.twofa_empty_label.setVisible(True)
        self.twofa_active_container.setVisible(False)
        self.totp_secret_edit.setFocus()

    def _configure_oauth_manager(self) -> None:
        client_id = str(self.settings.get("microsoft_client_id", "")).strip()
        self.microsoft_oauth = None
        if MSAL_AVAILABLE and valid_client_id(client_id):
            try:
                self.microsoft_oauth = MicrosoftOAuthManager(client_id, MICROSOFT_TOKEN_CACHE)
            except OAuthError:
                self.microsoft_oauth = None

    def _token_for_account(self, account: Account) -> str:
        if self.microsoft_oauth is None:
            return ""
        try:
            return self.microsoft_oauth.get_access_token(account.email)
        except OAuthError:
            return ""

    def _setup_microsoft_oauth(self) -> bool:
        if not MSAL_AVAILABLE:
            dependency_message = (
                "Microsoft sign-in support is missing from this Theara build. "
                "Reinstall or rebuild the application."
                if getattr(sys, "frozen", False)
                else "Install the required package with: python -m pip install -r requirements.txt"
            )
            QMessageBox.warning(
                self,
                "Microsoft OAuth dependency",
                dependency_message,
            )
            return False
        current = str(self.settings.get("microsoft_client_id", ""))
        dialog = MicrosoftSetupDialog(current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.settings["microsoft_client_id"] = dialog.client_id.text().strip()
        try:
            save_settings(SETTINGS_FILE, self.settings)
        except OSError:
            QMessageBox.warning(self, "Settings", "Could not save the Microsoft client ID.")
            return False
        self._configure_oauth_manager()
        return self.microsoft_oauth is not None

    def _selected_microsoft_account(self) -> Account | None:
        rows = sorted(
            {
                index.row()
                for index in self.table.selectionModel().selectedRows()
                if not self.table.isRowHidden(index.row())
            }
        )
        candidates = (
            [self.accounts[row] for row in rows]
            if rows
            else self._accounts_for_current_mail_view()
        )
        for account in candidates:
            resolved = detect_provider(account.email) if account.provider == "auto" else account.provider
            if resolved == "microsoft":
                return account
        return None

    def _connect_microsoft(self) -> None:
        account = self._selected_microsoft_account()
        if account is None:
            QMessageBox.information(
                self,
                "Connect Microsoft",
                "Add or select an Outlook, Hotmail, Live, or MSN mailbox first.",
            )
            return
        if self.microsoft_oauth is None and not self._setup_microsoft_oauth():
            return
        try:
            flow = self.microsoft_oauth.initiate_device_flow()  # type: ignore[union-attr]
        except OAuthError as exc:
            QMessageBox.warning(self, "Microsoft sign-in", str(exc))
            return

        dialog = DeviceCodeDialog(flow, self)
        self._oauth_dialog = dialog
        self._oauth_target = account
        self._oauth_cancel_event = threading.Event()
        self._oauth_result: dict | None = None
        task = OAuthTask(self.microsoft_oauth, flow, self._oauth_cancel_event)  # type: ignore[arg-type]
        task.signals.completed.connect(self._oauth_completed)
        self.thread_pool.start(task)
        QDesktopServices.openUrl(
            QUrl(str(flow.get("verification_uri", "https://microsoft.com/devicelogin")))
        )
        dialog_result = dialog.exec()
        if dialog_result != QDialog.DialogCode.Accepted:
            self._oauth_cancel_event.set()
        result = self._oauth_result
        self._oauth_dialog = None
        if result and result.get("success"):
            signed_in = str(result.get("email", ""))
            if signed_in and signed_in.casefold() != account.email.casefold():
                QMessageBox.warning(
                    self,
                    "Different Microsoft account",
                    f"Microsoft connected {signed_in}, but the selected row is {account.email}. "
                    "Reconnect and choose the matching mailbox.",
                )
                return
            self.statusBar().showMessage(f"Microsoft connected for {account.email}. Fetching inbox…", 7000)
            QTimer.singleShot(200, lambda: self._start_fetch([account]))
        elif result:
            QMessageBox.warning(
                self,
                "Microsoft sign-in",
                str(result.get("detail", "Microsoft sign-in was not completed.")),
            )

    @Slot(object)
    def _oauth_completed(self, result: dict) -> None:
        self._oauth_result = result
        if self._oauth_dialog is None:
            return
        if result.get("success"):
            self._oauth_dialog.status.setText("Microsoft sign-in complete.")
            self._oauth_dialog.accept()
        else:
            self._oauth_dialog.status.setText(str(result.get("detail", "Sign-in was not completed.")))
            self._oauth_dialog.reject()

    def _load_saved_accounts(self) -> None:
        try:
            saved = load_accounts(ACCOUNT_STORE)
            self.accounts = [
                Account(
                    item["email"],
                    item["password"],
                    item.get("provider", "auto"),
                    item.get("imap_host", ""),
                    item.get("mail_alias", ""),
                )
                for item in saved
            ]
            self._rebuild_table()
            if self.accounts:
                self.statusBar().showMessage(f"Loaded {len(self.accounts)} securely saved account(s).", 5000)
        except SecureStoreError:
            self._rebuild_table()

        # Always check adjacent accounts.txt to auto-import or refresh passwords
        self._auto_import_adjacent_accounts()

    def _auto_import_adjacent_accounts(self) -> None:
        """Check for accounts.txt or accounts.csv next to Theara.exe and auto-import/update."""
        for candidate_name in ("accounts.txt", "accounts.csv", "yandex.txt"):
            for search_dir in (APP_BINARY_DIR, SOURCE_DIR, Path.cwd()):
                candidate_path = search_dir / candidate_name
                if candidate_path.is_file():
                    try:
                        text = candidate_path.read_text(encoding="utf-8-sig")
                        accounts, _errors = parse_account_lines(text)
                        if accounts:
                            added, updated = self._add_accounts(accounts)
                            if added or updated:
                                self.statusBar().showMessage(
                                    f"Loaded {len(self.accounts)} account(s) (Updated: {updated}, Added: {added}) from {candidate_name}.", 7000
                                )
                                return
                    except Exception:
                        pass
        if not self.accounts:
            self.statusBar().showMessage("Add, paste, or import accounts to begin.")

    def _save_accounts(self) -> bool:
        try:
            save_accounts(
                ACCOUNT_STORE,
                (
                    {
                        "email": account.email,
                        "password": account.password,
                        "provider": account.provider,
                        "imap_host": account.imap_host,
                        "mail_alias": account.mail_alias,
                    }
                    for account in self.accounts
                ),
            )
            return True
        except (SecureStoreError, OSError) as exc:
            QMessageBox.warning(self, "Could not save accounts", str(exc))
            return False

    def _rebuild_table(self) -> None:
        self.table.setRowCount(0)
        for account in self.accounts:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if self.select_all.isChecked() else Qt.CheckState.Unchecked)
            self.table.setItem(row, self.COL_CHECK, check)
            self.table.setItem(row, self.COL_EMAIL, QTableWidgetItem(account.email))
            resolved_provider = detect_provider(account.email) if account.provider == "auto" else account.provider
            provider_item = QTableWidgetItem(provider_label(resolved_provider))
            provider_item.setToolTip(account.imap_host or "Provider default server")
            self.table.setItem(row, self.COL_PROVIDER, provider_item)
            masked = QTableWidgetItem(
                "OAuth" if resolved_provider == "microsoft" else mask_password(account.password)
            )
            masked.setForeground(QColor("#77766f"))
            masked.setToolTip(
                "Microsoft tokens are Windows-encrypted."
                if resolved_provider == "microsoft"
                else "Stored with Windows encryption; the password is never shown in the table."
            )
            self.table.setItem(row, self.COL_PASSWORD, masked)
            result = self.results.get(account.email.casefold())
            if result:
                self._render_result(row, result)
            else:
                self._set_item(row, self.COL_SENDER, "—", "#77766f")
                self._set_item(row, self.COL_SUBJECT, "Not checked yet", "#77766f")
                self._set_item(row, self.COL_RECEIVED, "—", "#77766f")
                self._set_item(row, self.COL_STATUS, "Ready", "#c3b77f")
        self._update_metrics()
        self._rebuild_recent_mail()
        self._filter_rows(self.filter_edit.text())

    def _set_item(self, row: int, column: int, text: str, color: str | None = None) -> QTableWidgetItem:
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, column, item)
        item.setText(text)
        apply_khmer_font(item, text)
        if color:
            item.setForeground(QColor(color))
        return item

    def _selected_account_keys(self) -> set[str]:
        """Return the current account identifier without reading display text."""
        row = self.table.currentRow()
        if 0 <= row < len(self.accounts):
            return {self.accounts[row].email.casefold()}
        return set()

    def _account_selection_changed(self, *_coordinates: int) -> None:
        # All Mail deliberately remains an inbox-wide list when an account row
        # is selected.  The account selection is still used by other actions.
        return

    def _focus_recent_filter(self) -> None:
        """Focus the useful search/input field for the active workspace."""
        if self._current_view == self.VIEW_2FA:
            self.totp_secret_edit.setFocus()
            self.totp_secret_edit.selectAll()
            return
        self.recent_filter_edit.setFocus()
        self.recent_filter_edit.selectAll()

    def _rebuild_recent_mail(self) -> None:
        """Render every fetched message from every account."""
        self.recent_mail_table.setRowCount(0)
        mail_entries: list[tuple[float, Account, str, int, MailMessage]] = []
        for account in self.accounts:
            account_key = account.email.casefold()
            result = self.results.get(account_key)
            messages = tuple(getattr(result, "messages", ()) or ()) if result is not None else ()
            for message_index, message in enumerate(messages):
                mail_entries.append(
                    (
                        float(getattr(message, "received_timestamp", 0.0) or 0.0),
                        account,
                        account_key,
                        message_index,
                        message,
                    )
                )

        # Python's stable sort preserves account/message order for legacy
        # entries without timestamps. By default, code-bearing mail stays at
        # the top and remains newest-first inside each group.
        if self.codes_first_checkbox.isChecked():
            mail_entries.sort(
                key=lambda entry: (
                    bool(str(getattr(entry[4], "code", "") or "").strip()),
                    entry[0],
                ),
                reverse=True,
            )
        else:
            mail_entries.sort(key=lambda entry: entry[0], reverse=True)
        for _timestamp, account, account_key, message_index, message in mail_entries:
            row = self.recent_mail_table.rowCount()
            self.recent_mail_table.insertRow(row)

            submail = str(
                getattr(message, "recipient", "")
                or account.mail_alias
                or account.email
            )
            account_item = QTableWidgetItem(submail)
            account_item.setToolTip(f"Login mailbox: {account.email}")
            account_item.setData(self.MAIL_ACCOUNT_KEY_ROLE, account_key)
            account_item.setData(self.MAIL_MESSAGE_INDEX_ROLE, message_index)
            account_item.setData(self.MAIL_SEARCH_TEXT_ROLE, account.email)
            self.recent_mail_table.setItem(row, self.MAIL_COL_ACCOUNT, account_item)

            code = str(getattr(message, "code", "") or "")
            code_item = QTableWidgetItem(code or "—")
            code_item.setForeground(QColor("#f5c542" if code else "#77766f"))
            code_font = QFont("Consolas", 12)
            code_font.setBold(bool(code))
            code_item.setFont(code_font)
            code_item.setToolTip("Click to copy this code." if code else "No code in this message.")
            self.recent_mail_table.setItem(row, self.MAIL_COL_CODE, code_item)
            facebook_name = str(
                getattr(message, "facebook_name", "")
                or getattr(message, "sender", "")
                or "—"
            )
            facebook_item = QTableWidgetItem(facebook_name)
            original_sender = str(getattr(message, "sender", "") or "")
            facebook_item.setToolTip(original_sender)
            facebook_item.setData(self.MAIL_SEARCH_TEXT_ROLE, original_sender)
            apply_khmer_font(facebook_item, facebook_name)
            self.recent_mail_table.setItem(
                row,
                self.MAIL_COL_FACEBOOK_NAME,
                facebook_item,
            )
            subject_text = str(getattr(message, "subject", "") or "—")
            subject_item = QTableWidgetItem(subject_text)
            apply_khmer_font(subject_item, subject_text)
            self.recent_mail_table.setItem(
                row,
                self.MAIL_COL_SUBJECT,
                subject_item,
            )
            self.recent_mail_table.setItem(
                row,
                self.MAIL_COL_RECEIVED,
                QTableWidgetItem(str(getattr(message, "received", "") or "—")),
            )

        self._filter_recent_mail_rows()
        if self.recent_mail_table.rowCount() and not self._recent_columns_initialized:
            QTimer.singleShot(0, self._fit_recent_columns)

    def _recent_message_for_row(self, row: int) -> tuple[Account, MailMessage] | None:
        """Resolve safe row metadata back to the current in-memory message."""
        if row < 0 or row >= self.recent_mail_table.rowCount():
            return None
        account_item = self.recent_mail_table.item(row, self.MAIL_COL_ACCOUNT)
        if account_item is None:
            return None
        account_key = str(account_item.data(self.MAIL_ACCOUNT_KEY_ROLE) or "")
        try:
            message_index = int(account_item.data(self.MAIL_MESSAGE_INDEX_ROLE))
        except (TypeError, ValueError):
            return None
        account = next(
            (candidate for candidate in self.accounts if candidate.email.casefold() == account_key),
            None,
        )
        result = self.results.get(account_key)
        messages = tuple(getattr(result, "messages", ()) or ()) if result is not None else ()
        if account is None or message_index < 0 or message_index >= len(messages):
            return None
        return account, messages[message_index]

    def _copy_recent_mail_code(self, row: int, column: int) -> None:
        if column != self.MAIL_COL_CODE:
            return
        resolved_message = self._recent_message_for_row(row)
        if resolved_message is None:
            return
        code = str(getattr(resolved_message[1], "code", "") or "").strip()
        if not code:
            return

        QGuiApplication.clipboard().setText(code)
        code_item = self.recent_mail_table.item(row, self.MAIL_COL_CODE)
        if code_item is not None:
            cell_rect = self.recent_mail_table.visualItemRect(code_item)
            popup_position = self.recent_mail_table.viewport().mapToGlobal(cell_rect.center())
            QToolTip.showText(
                popup_position,
                f"Copied: {code}",
                self.recent_mail_table.viewport(),
                cell_rect,
                1800,
            )
        self.statusBar().showMessage(f"Copied code {code}.", 4000)

    def _recent_mail_double_clicked(self, row: int, _column: int) -> None:
        resolved_message = self._recent_message_for_row(row)
        if resolved_message is None:
            return
        self.recent_mail_table.selectRow(row)
        account, message = resolved_message
        resolved_provider = detect_provider(account.email) if account.provider == "auto" else account.provider
        dialog = MessageBodyDialog(account, message, provider_label(resolved_provider), self)
        dialog.exec()

    def _zoom_columns(self, factor: float) -> None:
        """Scale all data-column widths while keeping useful minimums."""
        header = self.table.horizontalHeader()
        for column in range(1, self.table.columnCount()):
            current = header.sectionSize(column)
            minimum = self.COLUMN_MIN_WIDTHS[column]
            header.resizeSection(column, max(minimum, min(700, round(current * factor))))
        mail_header = self.recent_mail_table.horizontalHeader()
        for column in range(self.recent_mail_table.columnCount()):
            current = mail_header.sectionSize(column)
            minimum = self.MAIL_COLUMN_MIN_WIDTHS[column]
            mail_header.resizeSection(column, max(minimum, min(700, round(current * factor))))

    def _fit_columns(self) -> None:
        """Fit the data columns to the current viewport using stable proportions."""
        header = self.table.horizontalHeader()
        check_width = self.COLUMN_DEFAULT_WIDTHS[self.COL_CHECK]
        header.resizeSection(self.COL_CHECK, check_width)
        available = max(1, self.table.viewport().width() - check_width - 3)
        base = sum(self.COLUMN_DEFAULT_WIDTHS[1:])
        widths = []
        for column in range(1, self.table.columnCount()):
            scaled = round(self.COLUMN_DEFAULT_WIDTHS[column] * available / base)
            widths.append(max(self.COLUMN_MIN_WIDTHS[column], scaled))
        remaining = available - sum(widths)
        if remaining > 0:
            widths[self.COL_SUBJECT - 1] += remaining
        for column, width in enumerate(widths, start=1):
            header.resizeSection(column, width)
        self._fit_recent_columns()

    def _fit_recent_columns(self) -> None:
        header = self.recent_mail_table.horizontalHeader()
        available = max(1, self.recent_mail_table.viewport().width() - 3)
        base = sum(self.MAIL_COLUMN_DEFAULT_WIDTHS)
        widths = [
            max(
                self.MAIL_COLUMN_MIN_WIDTHS[column],
                round(self.MAIL_COLUMN_DEFAULT_WIDTHS[column] * available / base),
            )
            for column in range(self.recent_mail_table.columnCount())
        ]
        remaining = available - sum(widths)
        if remaining > 0:
            widths[self.MAIL_COL_SUBJECT] += remaining
        for column, width in enumerate(widths):
            header.resizeSection(column, width)
        if self.recent_mail_table.rowCount():
            self._recent_columns_initialized = True

    def _add_accounts(self, incoming: list[Account]) -> tuple[int, int]:
        positions = {account.email.casefold(): index for index, account in enumerate(self.accounts)}
        added = 0
        updated = 0
        for account in incoming:
            key = account.email.casefold()
            if key in positions:
                existing = self.accounts[positions[key]]
                if not account.mail_alias and existing.mail_alias:
                    account = replace(account, mail_alias=existing.mail_alias)
                self.accounts[positions[key]] = account
                updated += 1
            else:
                positions[key] = len(self.accounts)
                self.accounts.append(account)
                added += 1
        if incoming:
            self._save_accounts()
            self._rebuild_table()
        return added, updated

    def _add_one(self) -> None:
        preferred_provider = (
            self._mail_view
            if self._current_view in {self.VIEW_YANDEX, self.VIEW_MICROSOFT}
            else ""
        )
        dialog = ProviderAccountDialog(self, preferred_provider=preferred_provider)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        account = dialog.account()
        added, updated = self._add_accounts([account])
        self.statusBar().showMessage(
            "Account added and encrypted." if added else "Account password updated and encrypted.", 5000
        )
        resolved = detect_provider(account.email) if account.provider == "auto" else account.provider
        if resolved == "microsoft":
            if self._mail_view != self.VIEW_MICROSOFT:
                self._switch_view(self.VIEW_MICROSOFT, announce=False)
            row = self._find_row(account.email)
            if row >= 0:
                self.table.selectRow(row)
            QTimer.singleShot(150, self._connect_microsoft)

    def _edit_selected_alias(self) -> None:
        """Edit the recipient alias for the first selected Yandex row."""
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        if not rows:
            self.statusBar().showMessage("Select a Yandex account to edit its alias.", 4000)
            return
        self._edit_mail_alias(rows[0])

    def _edit_mail_alias(self, row: int) -> None:
        if self._active or row < 0 or row >= len(self.accounts):
            return
        account = self.accounts[row]
        resolved = detect_provider(account.email) if account.provider == "auto" else account.provider
        if resolved != "yandex":
            QMessageBox.information(
                self,
                "Yandex alias",
                "Recipient alias preferences are available for Yandex accounts only.",
            )
            return
        alias, accepted = QInputDialog.getText(
            self,
            "Edit Yandex alias",
            (
                "Prefer a code addressed exactly to this alias in the upper table.\n"
                "All Mail will still list every recent address. Leave it blank to "
                "always use the newest code."
            ),
            QLineEdit.EchoMode.Normal,
            account.mail_alias,
        )
        if not accepted:
            return
        alias = alias.strip()
        valid, error = validate_mail_alias(account, alias)
        if not valid:
            QMessageBox.warning(self, "Invalid Yandex alias", error)
            return
        if alias == account.mail_alias:
            self.statusBar().showMessage("The Yandex alias was not changed.", 3000)
            return

        key = account.email.casefold()
        previous_result = self.results.pop(key, None)
        self.accounts[row] = replace(account, mail_alias=alias)
        if not self._save_accounts():
            self.accounts[row] = account
            if previous_result is not None:
                self.results[key] = previous_result
            return

        self._rebuild_table()
        self.table.selectRow(row)
        if alias:
            message = "Yandex alias preference updated. Fetch again to apply it."
        else:
            message = "Yandex alias preference cleared. Fetch again to use the newest code."
        self.statusBar().showMessage(message, 6000)

    def _paste_accounts(self) -> None:
        dialog = BulkImportDialog(self)
        clipboard = QGuiApplication.clipboard().text()
        if "@" in clipboard and "|" in clipboard:
            dialog.editor.setPlainText(clipboard)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        accounts, errors = parse_account_lines(dialog.text())
        if not accounts:
            detail = "\n".join(str(item) for item in errors[:8])
            QMessageBox.warning(self, "No valid accounts", detail or "Use email|app-password, one per line.")
            return
        added, updated = self._add_accounts(accounts)
        message = f"Added {added}; updated {updated}."
        if errors:
            message += f" Skipped {len(errors)} invalid line(s)."
        self.statusBar().showMessage(message, 7000)
        if errors:
            QMessageBox.information(
                self,
                "Import finished",
                message + "\n\n" + "\n".join(str(item) for item in errors[:8]),
            )

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import account list",
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DocumentsLocation
            ),
            "Text files (*.txt *.csv);;All files (*.*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read the selected file: {exc}")
            return
        accounts, errors = parse_account_lines(text)
        if not accounts:
            QMessageBox.warning(self, "No valid accounts", "No email|app-password lines were found.")
            return
        added, updated = self._add_accounts(accounts)
        self.statusBar().showMessage(
            f"Imported {added} new and updated {updated}; skipped {len(errors)} invalid line(s).",
            7000,
        )

    def _export_file(self) -> None:
        yandex_accounts = [
            acc for acc in self.accounts
            if (detect_provider(acc.email) if acc.provider == "auto" else acc.provider) != "microsoft"
        ]
        if not yandex_accounts:
            self.statusBar().showMessage("No mail accounts to export.", 4000)
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export account list",
            str(Path.home() / "Desktop" / "accounts.txt"),
            "Text files (*.txt);;CSV files (*.csv);;All files (*.*)",
        )
        if not path:
            return
        lines = []
        for acc in yandex_accounts:
            line = f"{acc.email}|{acc.password}"
            if acc.mail_alias:
                line += f"|{acc.mail_alias}"
            lines.append(line)
        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.statusBar().showMessage(
                f"Exported {len(lines)} account(s) to {Path(path).name}.", 6000
            )
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write file: {exc}")

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            self.statusBar().showMessage("Select one or more table rows to remove.", 4000)
            return
        answer = QMessageBox.question(
            self,
            "Remove accounts",
            f"Remove {len(rows)} selected account(s) from encrypted storage?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for row in rows:
            account = self.accounts.pop(row)
            self.results.pop(account.email.casefold(), None)
        self._save_accounts()
        self._rebuild_table()
        self.statusBar().showMessage(f"Removed {len(rows)} account(s).", 5000)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.item(row, self.COL_CHECK).setCheckState(state)

    def _checked_accounts(self) -> list[Account]:
        selected: list[Account] = []
        for row, account in enumerate(self.accounts):
            item = self.table.item(row, self.COL_CHECK)
            if item and item.checkState() == Qt.CheckState.Checked and not self.table.isRowHidden(row):
                selected.append(account)
        return selected

    def _fetch_selected(self) -> None:
        accounts = self._checked_accounts()
        if not accounts:
            rows = sorted(
                {
                    index.row()
                    for index in self.table.selectionModel().selectedRows()
                    if not self.table.isRowHidden(index.row())
                }
            )
            accounts = [self.accounts[row] for row in rows]
        if not accounts:
            self.statusBar().showMessage("Check or select at least one account.", 5000)
            return
        self._start_fetch(accounts)

    def _fetch_all(self) -> None:
        accounts = self._accounts_for_current_mail_view()
        if not accounts:
            provider = (
                "Hotmail or Outlook"
                if self._mail_view == self.VIEW_MICROSOFT
                else "Yandex"
            )
            self.statusBar().showMessage(
                f"Add at least one {provider} mail account first.", 5000
            )
            return
        self._start_fetch(accounts)

    def _selected_yandex_accounts(self) -> tuple[list[Account], int, int]:
        selected_rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()}
        )
        rows = [row for row in selected_rows if not self.table.isRowHidden(row)]
        hidden_count = len(selected_rows) - len(rows)
        selected = [self.accounts[row] for row in rows]
        yandex_accounts = [
            account
            for account in selected
            if (
                detect_provider(account.email)
                if account.provider == "auto"
                else account.provider
            )
            == "yandex"
        ]
        return yandex_accounts, len(selected) - len(yandex_accounts), hidden_count

    def _delete_all_yandex_mail_for_selection(self) -> None:
        if self._active:
            return
        accounts, ignored_count, hidden_count = self._selected_yandex_accounts()
        if not accounts:
            self.statusBar().showMessage(
                "Select one or more Yandex account rows first.",
                5000,
            )
            return

        shown = "\n".join(f"  - {account.email}" for account in accounts[:8])
        if len(accounts) > 8:
            shown += f"\n  - and {len(accounts) - 8} more"
        ignored_note = (
            f"\n\n{ignored_count} selected non-Yandex account(s) will be ignored."
            if ignored_count
            else ""
        )
        hidden_note = (
            f"\n{hidden_count} selected hidden account(s) will be excluded."
            if hidden_count
            else ""
        )
        prompt = (
            "PERMANENTLY DELETE ALL MAIL for:\n"
            f"{shown}{ignored_note}{hidden_note}\n\n"
            "This permanently empties every real selectable folder, including "
            "Inbox, Subscriptions, Social, Sent, Drafts/templates, Outbox, Archive, "
            "Spam, Trash, and custom folders. Messages shown in With attachments "
            "disappear when their stored copies are deleted.\n\n"
            "Messages cannot be restored. Folder structure, labels, rules, and the "
            "saved account remain. Any Yandex alias preference is ignored. Mail delivered "
            "after a folder snapshot may remain.\n\nType DELETE ALL MAIL to continue:"
        )
        confirmation, accepted = QInputDialog.getText(
            self,
            "Permanently delete all Yandex mail",
            prompt,
        )
        if not accepted:
            self.statusBar().showMessage("Yandex mail deletion canceled.", 4000)
            return
        if confirmation.strip() != "DELETE ALL MAIL":
            QMessageBox.information(
                self,
                "Delete canceled",
                "Nothing was changed because the confirmation text did not match "
                "DELETE ALL MAIL.",
            )
            return
        self._start_delete_all_yandex_mail(accounts)

    def _start_delete_all_yandex_mail(self, accounts: list[Account]) -> None:
        if self._active or not accounts:
            return
        self._run_id += 1
        self._cancel_event = threading.Event()
        self._pending = {account.email.casefold() for account in accounts}
        self._active = True
        self._active_action = "delete_all_mail"
        self._run_total = len(accounts)
        self._run_completed = 0
        self._delete_total_deleted = 0
        self._delete_total_folders = 0
        self._delete_successes = 0
        self._delete_partials = 0
        self._delete_unknowns = 0
        self._set_fetch_controls(True, can_stop=False)
        self.checked_card.value.setText(f"0 / {self._run_total}")
        # Some Yandex domains can be aliases for the same underlying mailbox.
        # Serial execution prevents two destructive sessions racing in that case.
        self.thread_pool.setMaxThreadCount(1)

        for account in accounts:
            row = self._find_row(account.email)
            if row >= 0:
                status_item = self._set_item(
                    row,
                    self.COL_STATUS,
                    "Deleting all mail...",
                    "#f5c542",
                )
                status_item.setToolTip("")
            task = DeleteAllMailTask(self._run_id, account, self._cancel_event)
            task.signals.completed.connect(self._delete_all_yandex_mail_completed)
            self.thread_pool.start(task)
        self.statusBar().showMessage(
            f"Permanently deleting all mail from {len(accounts)} selected Yandex "
            "mailbox(es)..."
        )

    @Slot(int, str, object)
    def _delete_all_yandex_mail_completed(
        self,
        run_id: int,
        address: str,
        result: DeleteMailResult,
    ) -> None:
        if (
            run_id != self._run_id
            or not self._active
            or self._active_action != "delete_all_mail"
        ):
            return
        key = address.casefold()
        self._pending.discard(key)
        status = getattr(result, "status", "connection_error")
        detail = getattr(result, "detail", "") or ""
        deleted_count = max(0, int(getattr(result, "deleted_count", 0) or 0))
        folder_count = max(0, int(getattr(result, "folder_count", 0) or 0))
        row = self._find_row(address)

        if status in {"deleted", "empty"}:
            self.results.pop(key, None)
            self._delete_successes += 1
            self._delete_total_deleted += deleted_count
            self._delete_total_folders += folder_count
            if row >= 0:
                self._set_item(row, self.COL_SENDER, "—", "#77766f")
                self._set_item(
                    row,
                    self.COL_SUBJECT,
                    "All mailbox folders emptied"
                    if status == "deleted"
                    else "Mailbox already empty",
                    "#d6d2c8",
                ).setToolTip(detail)
                self._set_item(row, self.COL_RECEIVED, "—", "#77766f")
                status_item = self._set_item(
                    row,
                    self.COL_STATUS,
                    f"{deleted_count} deleted" if status == "deleted" else "Already empty",
                    "#78d68b",
                )
                status_item.setToolTip(detail)
        elif status in {"partial", "outcome_unknown"}:
            self.results.pop(key, None)
            self._delete_total_deleted += deleted_count
            self._delete_total_folders += folder_count
            if status == "partial":
                self._delete_partials += 1
            else:
                self._delete_unknowns += 1
            if row >= 0:
                self._set_item(row, self.COL_SENDER, "—", "#77766f")
                self._set_item(
                    row,
                    self.COL_SUBJECT,
                    "Some folders may not be empty"
                    if status == "partial"
                    else "Check every Yandex folder",
                    "#d2b667",
                ).setToolTip(detail)
                self._set_item(row, self.COL_RECEIVED, "—", "#77766f")
                status_item = self._set_item(
                    row,
                    self.COL_STATUS,
                    "Partially deleted" if status == "partial" else "Check mailbox",
                    "#d2b667",
                )
                status_item.setToolTip(detail)
        elif row >= 0:
            labels = {
                "auth_error": "Login failed",
                "unsupported_provider": "Yandex only",
            }
            status_item = self._set_item(
                row,
                self.COL_STATUS,
                labels.get(status, "Delete failed"),
                "#ff8d82",
            )
            status_item.setToolTip(detail)

        self._run_completed += 1
        self.checked_card.value.setText(f"{self._run_completed} / {self._run_total}")
        self._rebuild_recent_mail()
        self._update_metrics()
        self._filter_rows(self.filter_edit.text())

        if not self._pending:
            failures = (
                self._run_total
                - self._delete_successes
                - self._delete_partials
                - self._delete_unknowns
            )
            summary = (
                f"Permanently deleted {self._delete_total_deleted} Yandex message(s) "
                f"from {self._delete_total_folders} folder(s) for "
                f"{self._delete_successes} account(s)."
            )
            if self._delete_partials:
                summary += (
                    f" {self._delete_partials} account(s) were only partially emptied; "
                    "check every folder before retrying."
                )
            if self._delete_unknowns:
                summary += (
                    f" {self._delete_unknowns} result(s) could not be confirmed; "
                    "check every folder before retrying."
                )
            if failures:
                summary += f" {failures} account(s) failed; see the STATUS tooltip."
            self._active = False
            self._active_action = ""
            self.thread_pool.setMaxThreadCount(4)
            self._set_fetch_controls(False)
            self._update_metrics()
            self.statusBar().showMessage(summary, 10000)

    def _start_fetch(self, accounts: list[Account]) -> None:
        if self._active:
            return
        self._run_id += 1
        self._cancel_event = threading.Event()
        self._pending = {account.email.casefold() for account in accounts}
        self._active = True
        self._active_action = "fetch"
        self._run_total = len(accounts)
        self._run_completed = 0
        self._set_fetch_controls(True)
        self.checked_card.value.setText(f"0 / {self._run_total}")
        for account in accounts:
            self.results.pop(account.email.casefold(), None)
        self._rebuild_recent_mail()
        self._update_metrics()
        for account in accounts:
            row = self._find_row(account.email)
            if row >= 0:
                status_item = self._set_item(row, self.COL_STATUS, "Connecting…", "#f5c542")
                status_item.setToolTip("")
                self._set_item(row, self.COL_SENDER, "—", "#77766f")
                subject_item = self._set_item(row, self.COL_SUBJECT, "Checking…", "#77766f")
                subject_item.setToolTip("")
                self._set_item(row, self.COL_RECEIVED, "—", "#77766f")
            task = FetchTask(
                self._run_id,
                account,
                int(self.age_combo.currentData()),
                self._cancel_event,
                self._token_for_account,
            )
            task.signals.completed.connect(self._fetch_completed)
            self.thread_pool.start(task)
        self.statusBar().showMessage(f"Checking {len(accounts)} mailbox(es) securely over IMAP…")

    @Slot(int, str, object)
    def _fetch_completed(self, run_id: int, address: str, result: FetchResult) -> None:
        if run_id != self._run_id or not self._active:
            return
        key = address.casefold()
        self.results[key] = result
        self._pending.discard(key)
        row = self._find_row(address)
        if row >= 0:
            self._render_result(row, result)
        self._rebuild_recent_mail()
        self._run_completed += 1
        self.checked_card.value.setText(f"{self._run_completed} / {self._run_total}")
        self._update_metrics()
        self._filter_rows(self.filter_edit.text())
        if not self._pending:
            found = sum(1 for result in self.results.values() if result.status == "found")
            self.statusBar().showMessage(f"Finished. {found} code(s) available.", 8000)
            self._active = False
            self._active_action = ""
            self._set_fetch_controls(False)

    def _render_result(self, row: int, result: FetchResult) -> None:
        status = getattr(result, "status", "connection_error")
        sender = getattr(result, "sender", "") or "—"
        subject = getattr(result, "subject", "") or "—"
        received = getattr(result, "received", "") or "—"
        detail = getattr(result, "detail", "") or ""
        colors = {
            "found": "#78d68b",
            "not_found": "#d2b667",
            "auth_error": "#ff8d82",
            "connection_error": "#ff8d82",
            "oauth_required": "#79b8ff",
            "unsupported_provider": "#ff8d82",
        }
        labels = {
            "found": "Code found",
            "not_found": "No recent code",
            "auth_error": "Login failed",
            "connection_error": "Connection error",
            "oauth_required": "Connect Microsoft",
            "unsupported_provider": "Choose provider",
        }
        color = colors.get(status, "#aaa79f")
        self._set_item(row, self.COL_SENDER, sender, "#d6d2c8")
        subject_item = self._set_item(row, self.COL_SUBJECT, subject, "#d6d2c8")
        self._set_item(row, self.COL_RECEIVED, received, "#aaa79f")
        status_item = self._set_item(row, self.COL_STATUS, labels.get(status, status.replace("_", " ").title()), color)
        status_item.setToolTip(detail)
        subject_item.setToolTip(detail)

    def _find_row(self, address: str) -> int:
        key = address.casefold()
        for row, account in enumerate(self.accounts):
            if account.email.casefold() == key:
                return row
        return -1

    def _set_fetch_controls(self, active: bool, *, can_stop: bool = True) -> None:
        for widget in (
            self.add_button,
            self.paste_button,
            self.import_button,
            self.remove_button,
            self.alias_button,
            self.delete_all_yandex_button,
            self.microsoft_button,
            self.fetch_selected_button,
            self.fetch_all_button,
            self.age_combo,
        ):
            widget.setEnabled(not active)
        for button in self.view_buttons.values():
            button.setEnabled(not active)
        self.stop_button.setEnabled(active and can_stop)
        if not active:
            self._apply_mail_view_controls()

    def _stop_fetch(self) -> None:
        if not self._active or self._active_action != "fetch":
            return
        self._cancel_event.set()
        self.thread_pool.clear()
        for address in self._pending:
            row = self._find_row(address)
            if row >= 0:
                self._set_item(row, self.COL_STATUS, "Stopped", "#aaa79f")
        self._pending.clear()
        self._active = False
        self._active_action = ""
        self._run_id += 1
        self._set_fetch_controls(False)
        self.statusBar().showMessage("Fetch stopped. Active network calls will close after their timeout.", 7000)

    def _filter_rows(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self.table.rowCount()):
            result = (
                self.results.get(self.accounts[row].email.casefold())
                if row < len(self.accounts)
                else None
            )
            result_code = str(getattr(result, "code", "") or "")
            haystack = " ".join(
                [
                    self.table.item(row, column).text()
                    for column in range(1, self.table.columnCount())
                    if self.table.item(row, column)
                ]
                + [result_code]
            ).casefold()
            matches_provider = (
                row < len(self.accounts)
                and self._account_matches_mail_view(self.accounts[row])
            )
            matches_text = not needle or needle in haystack
            self.table.setRowHidden(row, not (matches_provider and matches_text))
        self._filter_recent_mail_rows(needle)

    def _recent_search_haystack(self, row: int, columns: Iterable[int]) -> str:
        """Return displayed and safe hidden metadata used by All Mail search."""
        values: list[str] = []
        for column in columns:
            item = self.recent_mail_table.item(row, column)
            if item is None:
                continue
            values.append(item.text())
            search_text = item.data(self.MAIL_SEARCH_TEXT_ROLE)
            if search_text:
                values.append(str(search_text))
        return " ".join(values).casefold()

    def _filter_recent_mail_rows(self, global_needle: str | None = None) -> None:
        if global_needle is None:
            global_needle = self.filter_edit.text().strip().casefold()
        recent_needle = self.recent_filter_edit.text().strip().casefold()
        visible_account_keys = {
            account.email.casefold() for account in self._accounts_for_current_mail_view()
        }
        visible_rows = 0
        for row in range(self.recent_mail_table.rowCount()):
            global_haystack = self._recent_search_haystack(
                row, range(self.recent_mail_table.columnCount())
            )
            recent_haystack = self._recent_search_haystack(
                row,
                (
                    self.MAIL_COL_SUBMAIL,
                    self.MAIL_COL_FACEBOOK_NAME,
                ),
            )
            account_item = self.recent_mail_table.item(row, self.MAIL_COL_ACCOUNT)
            account_key = (
                str(account_item.data(self.MAIL_ACCOUNT_KEY_ROLE) or "")
                if account_item is not None
                else ""
            )
            matches_provider = account_key in visible_account_keys
            matches_global = not global_needle or global_needle in global_haystack
            matches_recent = not recent_needle or recent_needle in recent_haystack
            hidden = not (matches_provider and matches_global and matches_recent)
            self.recent_mail_table.setRowHidden(row, hidden)
            if not hidden:
                visible_rows += 1

        total_rows = self.recent_mail_table.rowCount()
        if total_rows:
            self.recent_match_label.setText(f"{visible_rows} of {total_rows}")
        else:
            self.recent_match_label.setText("0 mail")

        if visible_rows:
            self.recent_empty_label.setVisible(False)
            return

        if total_rows and (global_needle or recent_needle):
            message = "No mail matches the current search."
        elif total_rows:
            message = "No mail is available for this workspace."
        else:
            message = "No mail fetched yet. Click Fetch all to load the selected time window."
        self.recent_empty_label.setText(message)
        self.recent_empty_label.setVisible(True)
        self.recent_mail_table.setVisible(True)

    def _update_metrics(self) -> None:
        scoped_accounts = self._accounts_for_current_mail_view()
        scoped_keys = {account.email.casefold() for account in scoped_accounts}
        self.account_card.value.setText(str(len(scoped_accounts)))
        found = sum(
            1
            for key, result in self.results.items()
            if key in scoped_keys and result.status == "found"
        )
        self.code_card.value.setText(str(found))
        if not self._active:
            checked = sum(1 for key in self.results if key in scoped_keys)
            self.checked_card.value.setText(str(checked) if checked else "—")

    def _selected_code(self) -> str:
        recent_rows = sorted(
            {index.row() for index in self.recent_mail_table.selectionModel().selectedRows()}
        )
        for row in recent_rows:
            resolved_message = self._recent_message_for_row(row)
            if resolved_message is not None:
                code = str(getattr(resolved_message[1], "code", "") or "")
                if code:
                    return code
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            if 0 <= row < len(self.accounts):
                account_key = self.accounts[row].email.casefold()
                result = self.results.get(account_key)
                code = str(getattr(result, "code", "") or "") if result is not None else ""
                if code:
                    return code
        return ""

    def _copy_selected_code(self) -> None:
        code = self._selected_code()
        if not code:
            self.statusBar().showMessage("Select a row that has a code.", 4000)
            return
        QGuiApplication.clipboard().setText(code)
        self.statusBar().showMessage(f"Copied code {code}.", 4000)

    def _open_message_body(self, row: int) -> None:
        if row < 0 or row >= len(self.accounts):
            return
        self.table.selectRow(row)
        account = self.accounts[row]
        result = self.results.get(account.email.casefold())
        if result is None or not getattr(result, "body", ""):
            self.statusBar().showMessage(
                "No fetched message is available for this row. Fetch the account first.",
                5000,
            )
            return
        resolved = detect_provider(account.email) if account.provider == "auto" else account.provider
        dialog = MessageBodyDialog(account, result, provider_label(resolved), self)
        dialog.exec()

    def _cell_double_clicked(self, row: int, _column: int) -> None:
        self._open_message_body(row)

    def _show_context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        copy_code = QAction("Copy code", self)
        copy_email = QAction("Copy email", self)
        edit_alias = QAction("Edit Yandex alias...", self)
        copy_alias = QAction("Copy Yandex alias", self)
        delete_all_yandex_mail_action = QAction(
            "Permanently delete all mail...",
            self,
        )
        view_message = QAction("View message body", self)
        fetch = QAction("Fetch this account", self)
        connect_microsoft = QAction("Connect Microsoft", self)
        row_result = self.results.get(self.accounts[row].email.casefold())
        copy_code.setEnabled(bool(row_result and getattr(row_result, "code", "")))
        copy_code.triggered.connect(self._copy_selected_code)
        copy_email.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(self.accounts[row].email)
        )
        view_message.setEnabled(bool(row_result and getattr(row_result, "body", "")))
        view_message.triggered.connect(lambda: self._open_message_body(row))
        fetch.triggered.connect(lambda: self._start_fetch([self.accounts[row]]))
        fetch.setEnabled(not self._active)
        resolved = (
            detect_provider(self.accounts[row].email)
            if self.accounts[row].provider == "auto"
            else self.accounts[row].provider
        )
        edit_alias.setEnabled(resolved == "yandex" and not self._active)
        edit_alias.triggered.connect(lambda: self._edit_mail_alias(row))
        copy_alias.setEnabled(resolved == "yandex" and bool(self.accounts[row].mail_alias))
        copy_alias.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(self.accounts[row].mail_alias)
        )
        delete_all_yandex_mail_action.setEnabled(
            resolved == "yandex" and not self._active
        )
        delete_all_yandex_mail_action.triggered.connect(
            self._delete_all_yandex_mail_for_selection
        )
        connect_microsoft.setEnabled(resolved == "microsoft" and not self._active)
        connect_microsoft.triggered.connect(self._connect_microsoft)
        menu.addAction(view_message)
        menu.addAction(copy_code)
        menu.addAction(copy_email)
        if resolved == "yandex":
            menu.addAction(copy_alias)
            menu.addAction(edit_alias)
            menu.addAction(delete_all_yandex_mail_action)
        menu.addSeparator()
        menu.addAction(fetch)
        if resolved == "microsoft":
            menu.addAction(connect_microsoft)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _check_for_updates_interactive(self, manual: bool = False) -> None:
        if manual:
            self.update_check_button.setEnabled(False)
            self.update_check_button.setText("Checking...")
            self.statusBar().showMessage("Checking for updates online...", 3000)

        self._update_task = UpdateCheckTask()
        self._update_task.signals.found.connect(lambda update_info: self._on_update_found(update_info, manual))
        self._update_task.signals.not_found.connect(lambda: self._on_update_not_found(manual))
        self._update_task.signals.failed.connect(lambda err: self._on_update_failed(err, manual))
        self.thread_pool.start(self._update_task)

    @Slot(object, bool)
    def _on_update_found(self, update_info: UpdateInfo, manual: bool) -> None:
        self.update_check_button.setEnabled(True)
        self.update_check_button.setText(f"🚀 Update: v{update_info.version}")
        self.update_check_button.setStyleSheet("background-color: #f5c542; color: #101112; font-weight: bold;")
        self.statusBar().showMessage(f"New update v{update_info.version} available!", 6000)

        dialog = UpdateNotificationDialog(update_info, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            progress_dialog = DownloadProgressDialog(update_info, self.thread_pool, self)
            progress_dialog.exec()

    @Slot(bool)
    def _on_update_not_found(self, manual: bool) -> None:
        self.update_check_button.setEnabled(True)
        self.update_check_button.setText("Check update")
        if manual:
            self.statusBar().showMessage(f"You are using the latest version of Theara (v{APP_VERSION}).", 5000)
            QMessageBox.information(
                self,
                "Up to date",
                f"You are already using the latest version of Theara (v{APP_VERSION}).",
            )

    @Slot(str, bool)
    def _on_update_failed(self, error: str, manual: bool) -> None:
        self.update_check_button.setEnabled(True)
        self.update_check_button.setText("Check update")
        if manual:
            self.statusBar().showMessage(f"Update check failed: {error}", 5000)
            QMessageBox.warning(
                self,
                "Check for updates",
                f"Could not connect to update server:\n{error}",
            )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        super().resizeEvent(event)
        self._fit_columns()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if self._active and self._active_action == "delete_all_mail":
            QMessageBox.information(
                self,
                "Yandex deletion in progress",
                "Wait for the current permanent all-folder deletion to finish before "
                "closing the app.",
            )
            event.ignore()
            return
        self._cancel_event.set()
        self._oauth_cancel_event.set()
        self.thread_pool.clear()
        self.thread_pool.waitForDone(3000)
        event.accept()


def main() -> int:
    if sys.platform == "win32":
        os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    smoke_test = "--smoke-test" in sys.argv
    qt_arguments = [argument for argument in sys.argv if argument != "--smoke-test"]
    app = QApplication(qt_arguments)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("Theara Mail & 2FA Code Tool")
    app.setOrganizationName("Theara")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setStyle("Fusion")
    configure_application_fonts(app)
    app.setStyleSheet(STYLE)
    migrate_legacy_app_data()
    ensure_desktop_shortcut()
    window = MainWindow()
    window.show()
    if smoke_test:
        QTimer.singleShot(250, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
