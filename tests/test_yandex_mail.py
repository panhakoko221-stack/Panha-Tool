from __future__ import annotations

from email.message import EmailMessage
from email import policy
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
import imaplib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yandex_mail import (  # noqa: E402
    Account,
    FetchResult,
    MailMessage,
    PROVIDER_SPECS,
    ZOHO_IMAP_PRESETS,
    decode_message_body,
    decode_mime_header,
    detect_provider,
    extract_facebook_name,
    extract_verification_code,
    delete_all_yandex_mail,
    fetch_latest_code,
    parse_account_lines,
    provider_label,
)


def message_bytes(
    subject: str,
    body: str,
    *,
    html: bool = False,
    date: str = "Sat, 22 Aug 2026 12:00:00 +0000",
    recipient_headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Yandex <noreply@yandex.com>"
    message["Date"] = date
    for header_name, header_value in recipient_headers:
        message[header_name] = header_value
    if html:
        message.set_content("This message has an HTML version.")
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes(policy=policy.SMTP)


class AccountParsingTests(unittest.TestCase):
    def test_parse_split_once_dedupe_and_report_safe_line_numbers(self) -> None:
        source = "\n".join(
            (
                "User@Yandex.com|first|password",
                "user@yandex.com|duplicate",
                "missing delimiter secret",
                "bad-address|do-not-echo-me",
                "empty@yandex.com|",
            )
        )
        accounts, errors = parse_account_lines(source)

        self.assertEqual(accounts, [Account("User@Yandex.com", "first|password")])
        self.assertEqual([error.split(":", 1)[0] for error in errors], ["Line 3", "Line 4", "Line 5"])
        self.assertNotIn("do-not-echo-me", " ".join(errors))
        self.assertNotIn("first|password", repr(accounts[0]))
        self.assertIn("<redacted>", repr(accounts[0]))

    def test_provider_prefixes_preserve_pipe_passwords_and_reject_microsoft(self) -> None:
        source = "\n".join(
            (
                "yandex|alias@company.example|first|password",
                "zoho-mail|hosted@company.example|second|password",
                "microsoft|person@company.example|never-store-one",
                "person@hotmail.com|never-store-two",
                "outlook|person@elsewhere.example|never-store-three",
                "zoho|missing-password-field",
            )
        )

        accounts, errors = parse_account_lines(source)

        self.assertEqual(
            accounts,
            [
                Account(
                    "alias@company.example",
                    "first|password",
                    provider="yandex",
                ),
                Account(
                    "hosted@company.example",
                    "second|password",
                    provider="zoho",
                ),
            ],
        )
        self.assertEqual(
            [error.split(":", 1)[0] for error in errors],
            ["Line 3", "Line 4", "Line 5", "Line 6"],
        )
        combined_errors = " ".join(errors)
        for secret in ("never-store-one", "never-store-two", "never-store-three"):
            self.assertNotIn(secret, combined_errors)
        self.assertIn("OAuth", combined_errors)


class ProviderDetectionTests(unittest.TestCase):
    def test_detects_exact_provider_domains_case_insensitively(self) -> None:
        cases = {
            "person@YANDEX.COM": "yandex",
            "person@ya.ru": "yandex",
            "person@zoho.com": "zoho",
            "person@zohomail.eu": "zoho",
            "person@outlook.com": "microsoft",
            "person@hotmail.com": "microsoft",
            "person@live.com": "microsoft",
            "person@msn.com": "microsoft",
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(detect_provider(address), expected)

    def test_rejects_unknown_and_provider_suffix_spoofs(self) -> None:
        for address in (
            "person@example.com",
            "person@yandex.com.evil.test",
            "person@outlook.com.evil.test",
            "person@elsewhere.test@yandex.com",
            "missing-at-sign",
        ):
            with self.subTest(address=address):
                self.assertEqual(detect_provider(address), "unsupported")

    def test_provider_registry_and_labels_are_stable(self) -> None:
        self.assertEqual(PROVIDER_SPECS["yandex"].imap_host, "imap.yandex.com")
        self.assertEqual(PROVIDER_SPECS["microsoft"].auth_method, "oauth2")
        self.assertEqual(provider_label("auto"), "Auto detect")
        self.assertEqual(provider_label("zoho"), "Zoho Mail")
        self.assertEqual(provider_label("outlook"), "Outlook / Hotmail")
        self.assertEqual(provider_label("not-real"), "Unsupported provider")


class MessageDecodingTests(unittest.TestCase):
    def test_decodes_mime_header(self) -> None:
        encoded = "=?utf-8?b?0JrQvtC0INC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGP?="
        self.assertEqual(decode_mime_header(encoded), "Код подтверждения")

    def test_decodes_html_and_drops_script_and_style(self) -> None:
        message = EmailMessage()
        message.set_content(
            "<html><head><style>.x{}</style></head><body>"
            "<p>Your code is <strong>1&nbsp;2 3 4 5 6</strong></p>"
            "<script>999999</script></body></html>",
            subtype="html",
        )
        body = decode_message_body(message)
        self.assertIn("Your code is 1\u00a02 3 4 5 6", body)
        self.assertNotIn("999999", body)
        self.assertNotIn(".x", body)


class CodeExtractionTests(unittest.TestCase):
    def test_explicit_and_spaced_codes(self) -> None:
        self.assertEqual(
            extract_verification_code("Security notice", "Your verification code is 1 2 3 4 5 6."),
            "123456",
        )
        self.assertEqual(
            extract_verification_code("654321 is your login code", ""),
            "654321",
        )

    def test_subject_context_allows_large_bare_html_style_code(self) -> None:
        self.assertEqual(extract_verification_code("Your confirmation code", "847291"), "847291")

    def test_facebook_code_to_confirm_subject_and_name_template(self) -> None:
        subject = "00316 is your code to confirm this email"
        body = "Hi សុខ លីម,\n\nHere's the code to enter in Facebook:\n00316"

        self.assertEqual(extract_verification_code(subject, body), "00316")
        self.assertEqual(
            extract_facebook_name(subject, "Facebook <registration@facebookmail.com>", body),
            "សុខ លីម",
        )

    def test_avoids_dates_order_numbers_and_unrelated_mail(self) -> None:
        self.assertIsNone(
            extract_verification_code(
                "Order shipped",
                "Order #87654321 shipped on 2026-08-23. Invoice 456789.",
            )
        )
        self.assertIsNone(extract_verification_code("Verification code", "Date: 20260823"))
        self.assertEqual(
            extract_verification_code(
                "Verification code",
                "Order #876543. Your code is 492731.",
            ),
            "492731",
        )


class _FakeIMAP:
    instances: list["_FakeIMAP"] = []
    login_names: list[str] = []
    auth_mechanisms: list[str] = []
    auth_payloads: list[bytes] = []
    messages: dict[bytes, bytes] = {}
    reject_local_part = True
    reject_all = False

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.fetch_queries: list[str] = []
        self.search_queries: list[tuple[str, ...]] = []
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.login_names = []
        cls.auth_mechanisms = []
        cls.auth_payloads = []
        cls.messages = {}
        cls.reject_local_part = True
        cls.reject_all = False

    def login(self, login_name: str, password: str):
        self.__class__.login_names.append(login_name)
        if self.reject_all or (self.reject_local_part and "@" not in login_name):
            raise imaplib.IMAP4.error("authentication failed")
        return "OK", [b"authenticated"]

    def authenticate(self, mechanism: str, auth_object):
        self.__class__.auth_mechanisms.append(mechanism)
        payload = auth_object(b"")
        self.__class__.auth_payloads.append(payload)
        if self.reject_all:
            raise imaplib.IMAP4.error("oauth response included server-only diagnostics")
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False):
        if mailbox != "INBOX" or not readonly:
            return "NO", []
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria: str):
        self.search_queries.append(criteria)
        return "OK", [b" ".join(self.messages)]

    def fetch(self, message_set: bytes, query: str):
        self.fetch_queries.append(query)
        message_ids = message_set.split(b",")
        if query == "(INTERNALDATE)":
            return "OK", [message_id + b" (FLAGS ())" for message_id in message_ids]
        response = []
        for message_id in message_ids:
            raw = self.messages[message_id]
            response.extend(
                [
                    (message_id + b" (BODY[] {" + str(len(raw)).encode() + b"}", raw),
                    b")",
                ]
            )
        return "OK", response

    def logout(self):
        return "BYE", []


class _FakeDeleteIMAP:
    instances: list["_FakeDeleteIMAP"] = []
    login_names: list[str] = []
    advertised_capabilities = b"IMAP4rev1 UIDPLUS MOVE"
    folder_data = [
        b'(\\HasNoChildren) "|" "INBOX"',
        b'(\\HasNoChildren) "|" "Subscriptions"',
        b'(\\HasNoChildren) "|" "Social"',
        b'(\\HasNoChildren \\All) "|" "With attachments"',
        b'(\\HasChildren \\Noselect) "|" "My folders"',
        b'(\\HasNoChildren) "|" "My folders|Archive"',
        b'(\\HasNoChildren \\Sent) "|" "Sent"',
        b'(\\HasChildren \\Drafts) "|" "Drafts"',
        b'(\\HasNoChildren \\Junk) "|" "Spam"',
        b'(\\HasNoChildren \\Trash) "|" "Trash"',
    ]
    uids_by_mailbox = {
        b"INBOX": b"1 4",
        b"Subscriptions": b"5 9",
        b"Social": b"2 3 8",
        b"My folders|Archive": b"7",
        b"Sent": b"10",
        b"Drafts": b"",
        b"Spam": b"12",
        b"Trash": b"20 22",
    }
    reject_local_part = True
    fail_at: tuple[bytes, str] | None = None
    raise_at: tuple[bytes, str] | None = None
    invalid_search_at: tuple[bytes, str] | None = None
    inject_uid_after_search: dict[bytes, int] = {}

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.select_calls: list[tuple[object, bool]] = []
        self.uid_calls: list[tuple[bytes, object, ...]] = []
        self.list_calls = 0
        self.logged_out = False
        self.current_mailbox = b""
        self._injected: set[bytes] = set()
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.login_names = []
        cls.advertised_capabilities = b"IMAP4rev1 UIDPLUS MOVE"
        cls.folder_data = [
            b'(\\HasNoChildren) "|" "INBOX"',
            b'(\\HasNoChildren) "|" "Subscriptions"',
            b'(\\HasNoChildren) "|" "Social"',
            b'(\\HasNoChildren \\All) "|" "With attachments"',
            b'(\\HasChildren \\Noselect) "|" "My folders"',
            b'(\\HasNoChildren) "|" "My folders|Archive"',
            b'(\\HasNoChildren \\Sent) "|" "Sent"',
            b'(\\HasChildren \\Drafts) "|" "Drafts"',
            b'(\\HasNoChildren \\Junk) "|" "Spam"',
            b'(\\HasNoChildren \\Trash) "|" "Trash"',
        ]
        cls.uids_by_mailbox = {
            b"INBOX": b"1 4",
            b"Subscriptions": b"5 9",
            b"Social": b"2 3 8",
            b"My folders|Archive": b"7",
            b"Sent": b"10",
            b"Drafts": b"",
            b"Spam": b"12",
            b"Trash": b"20 22",
        }
        cls.reject_local_part = True
        cls.fail_at = None
        cls.raise_at = None
        cls.invalid_search_at = None
        cls.inject_uid_after_search = {}

    @staticmethod
    def _identity(mailbox: object) -> bytes:
        value = mailbox.encode("ascii") if isinstance(mailbox, str) else bytes(mailbox)
        value = value.strip()
        if value.startswith(b'"') and value.endswith(b'"'):
            value = value[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        return value

    @staticmethod
    def _uid_values(raw: bytes) -> list[int]:
        return [int(value) for value in raw.split()] if raw else []

    def login(self, login_name: str, _password: str):
        self.__class__.login_names.append(login_name)
        if self.reject_local_part and "@" not in login_name:
            raise imaplib.IMAP4.error("server authentication detail")
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False):
        self.select_calls.append((mailbox, readonly))
        identity = self._identity(mailbox)
        if self.fail_at == (identity, "SELECT"):
            return "NO", [b"server-only detail"]
        self.current_mailbox = identity
        messages = self.uids_by_mailbox.get(identity, b"")
        return "OK", [str(len(messages.split())).encode()]

    def uid(self, command: str, *args):
        call = (self.current_mailbox, command, *args)
        self.uid_calls.append(call)
        if command in {"MOVE", "COPY"}:
            raise AssertionError(f"permanent purge must not use {command}")
        if self.raise_at == (self.current_mailbox, command):
            raise RuntimeError("server-only exception detail")
        if self.fail_at == (self.current_mailbox, command):
            return "NO", [b"server-only detail"]
        if command == "SEARCH":
            raw_values = self.uids_by_mailbox.get(self.current_mailbox, b"")
            if args == (None, "ALL"):
                if self.invalid_search_at == (self.current_mailbox, "INITIAL"):
                    return "OK", [None]
                snapshot = raw_values
                injected = self.inject_uid_after_search.get(self.current_mailbox)
                if injected is not None and self.current_mailbox not in self._injected:
                    self._injected.add(self.current_mailbox)
                    values = self._uid_values(raw_values)
                    values.append(injected)
                    self.uids_by_mailbox[self.current_mailbox] = b" ".join(
                        str(value).encode() for value in values
                    )
                return "OK", [snapshot]
            if len(args) == 3 and args[:2] == (None, "UID"):
                if self.invalid_search_at == (self.current_mailbox, "VERIFY"):
                    return "OK", [None]
                values = self._uid_values(raw_values)
                highest = int(str(args[2]).split(":", 1)[1])
                remaining = [value for value in values if value <= highest]
                return "OK", [b" ".join(str(value).encode() for value in remaining)]
        if command == "EXPUNGE":
            highest = int(str(args[0]).split(":", 1)[1])
            values = self._uid_values(self.uids_by_mailbox.get(self.current_mailbox, b""))
            self.uids_by_mailbox[self.current_mailbox] = b" ".join(
                str(value).encode() for value in values if value > highest
            )
        return "OK", [b"done"]

    def list(self):
        self.list_calls += 1
        if self.fail_at == (b"", "LIST"):
            return "NO", [b"server-only detail"]
        return "OK", list(self.folder_data)

    def capability(self):
        return "OK", [self.advertised_capabilities]

    def expunge(self):
        raise AssertionError("permanent purge must use targeted UID EXPUNGE")

    def logout(self):
        self.logged_out = True
        return "BYE", []


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeIMAP.reset()

    def test_fetches_newest_with_personal_login_fallback_and_body_peek(self) -> None:
        _FakeIMAP.messages = {
            b"1": message_bytes("Receipt", "Order #123456"),
            b"2": message_bytes("Confirmation code", "9 1 7 4 2 0"),
        }
        account = Account("person@yandex.com", "super-secret")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account, max_age_hours=0)

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "917420")
        self.assertEqual(result.body, "9 1 7 4 2 0")
        self.assertEqual(_FakeIMAP.login_names, ["person", "person@yandex.com"])
        self.assertTrue(any("BODY.PEEK" in query for instance in _FakeIMAP.instances for query in instance.fetch_queries))
        self.assertNotIn(account.password, repr(result))
        self.assertNotIn(account.password, result.detail)

    def test_mail_alias_accepts_an_exact_recipient_header_match(self) -> None:
        alias = "aliasdemo+432teamC@yandex.com"
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Security code",
                "Your verification code is 432105",
                recipient_headers=(("Delivered-To", alias),),
            ),
        }
        account = Account(
            "aliasdemo@yandex.com",
            "app-password",
            mail_alias=alias,
        )

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account, max_age_hours=0)

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "432105")
        self.assertEqual(
            _FakeIMAP.login_names,
            ["aliasdemo", "aliasdemo@yandex.com"],
        )

    def test_mail_alias_skips_newer_nonmatch_and_body_substring_for_older_match(self) -> None:
        alias = "teambox+102teamB@yandex.com"
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Login code",
                "Your login code is 111222",
                recipient_headers=(("To", f"Team B <{alias}>"),),
            ),
            b"2": message_bytes(
                "Login code",
                f"Your login code is 999888 for {alias}",
                recipient_headers=(
                    ("To", "teambox+102teamB-extra@yandex.com"),
                ),
            ),
        }
        account = Account(
            "teambox@yandex.com",
            "app-password",
            mail_alias=alias,
        )

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account, max_age_hours=0)

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "111222")

    def test_mail_alias_matches_case_insensitively_across_decoded_headers(self) -> None:
        alias = "casebox+128teamd@yandex.com"
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Confirmation code",
                "Your confirmation code is 128704",
                recipient_headers=(
                    ("To", "Wrong Recipient <other@yandex.com>"),
                    ("X-Original-To", "not-the-alias@yandex.com"),
                    (
                        "Resent-To",
                        "=?utf-8?b?VGVhbSBE?= "
                        "<CASEBOX+128TEAMD@YANDEX.COM>",
                    ),
                ),
            ),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account(
                    "casebox@yandex.com",
                    "app-password",
                    mail_alias=alias,
                ),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "128704")

    def test_mail_alias_preserves_plus_address_for_exact_matching(self) -> None:
        alias = "plusbox+100teamA@yandex.com"
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Verification code",
                "Your verification code is 100700",
                recipient_headers=(
                    ("Apparently-To", f"Account Team <{alias}>"),
                ),
            ),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account(
                    "plusbox@yandex.com",
                    "app-password",
                    mail_alias=alias,
                ),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "100700")
        self.assertNotIn(alias, _FakeIMAP.login_names)

    def test_blank_mail_alias_keeps_any_recipient_behavior(self) -> None:
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Security code",
                "Your verification code is 246802",
                recipient_headers=(("To", "someone-else@yandex.com"),),
            ),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@yandex.com", "app-password"),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "246802")

    def test_fetch_result_messages_append_after_legacy_body_position(self) -> None:
        legacy = FetchResult(
            "found",
            "123456",
            "subject",
            "sender",
            "date",
            "detail",
            "legacy body",
        )
        body_text = "private message body"
        with_body = FetchResult("found", body=body_text)

        self.assertEqual(legacy.body, "legacy body")
        self.assertEqual(legacy.messages, ())
        self.assertEqual(with_body.body, body_text)
        self.assertNotIn(body_text, repr(with_body))

    def test_all_messages_are_newest_first_and_include_no_code_mail(self) -> None:
        _FakeIMAP.messages = {
            str(index).encode(): message_bytes(
                f"Newsletter {index:02d}",
                f"Ordinary update number {index}.",
            )
            for index in range(1, 26)
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@yandex.com", "app-password"),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(len(result.messages), 25)
        self.assertEqual(
            [message.subject for message in result.messages],
            [f"Newsletter {index:02d}" for index in range(25, 0, -1)],
        )
        self.assertTrue(all(message.code == "" for message in result.messages))

    def test_recent_messages_keep_newest_code_result_while_scanning_older_mail(self) -> None:
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Older security code",
                "Your verification code is 111111",
            ),
            b"2": message_bytes(
                "Newest security code",
                "Your verification code is 222222",
            ),
            b"3": message_bytes("Latest newsletter", "There is no code here."),
            b"4": message_bytes("Older newsletter", "Still no verification number."),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@yandex.com", "app-password"),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "222222")
        self.assertEqual(result.subject, "Newest security code")
        self.assertEqual(
            [message.subject for message in result.messages],
            [
                "Older newsletter",
                "Latest newsletter",
                "Newest security code",
                "Older security code",
            ],
        )
        self.assertEqual(
            [message.code for message in result.messages],
            ["", "", "222222", "111111"],
        )

    def test_alias_prefers_matching_code_without_hiding_other_mail(self) -> None:
        alias = "recentbox+20team@yandex.com"
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Matching code",
                "Your login code is 202020",
                recipient_headers=(("To", alias),),
            ),
            b"2": message_bytes(
                "Matching newsletter",
                "An ordinary team update.",
                recipient_headers=(("Delivered-To", alias.upper()),),
            ),
            b"3": message_bytes(
                "Other alias code",
                "Your login code is 303030",
                recipient_headers=(("To", "recentbox+other@yandex.com"),),
            ),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account(
                    "recentbox@yandex.com",
                    "app-password",
                    mail_alias=alias,
                ),
                max_age_hours=0,
            )

        self.assertEqual(result.code, "202020")
        self.assertEqual(
            [message.subject for message in result.messages],
            ["Other alias code", "Matching newsletter", "Matching code"],
        )

    def test_dynamic_facebook_submail_metadata_and_one_hour_cutoff(self) -> None:
        now = datetime.now(timezone.utc)
        actual_submail = "recentbox+d440413@yandex.com"
        _FakeIMAP.messages = {
            b"1": message_bytes(
                "Old newsletter",
                "This message is outside the selected window.",
                date=format_datetime(now - timedelta(hours=2)),
            ),
            b"2": message_bytes(
                "93605 is your code to confirm this email",
                "Hi Dara Sok,\n\nHere's the code to enter in Facebook:\n93605",
                date=format_datetime(now - timedelta(minutes=5)),
                recipient_headers=(("Delivered-To", actual_submail),),
            ),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account(
                    "recentbox@yandex.com",
                    "app-password",
                    mail_alias="recentbox+stale@yandex.com",
                ),
                max_messages=0,
                max_age_hours=1,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "93605")
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].recipient, actual_submail)
        self.assertEqual(result.messages[0].facebook_name, "Dara Sok")
        self.assertIn("configured alias did not match", result.detail.casefold())
        self.assertEqual(_FakeIMAP.instances[-1].search_queries[0][0], "SINCE")

    def test_recent_message_body_is_capped_and_excluded_from_repr(self) -> None:
        private_prefix = "PRIVATE-RECENT-BODY "
        oversized_body = private_prefix + ("x" * 205_000) + "END-OF-RECENT-BODY"
        _FakeIMAP.messages = {
            b"1": message_bytes("Ordinary private mail", oversized_body),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@yandex.com", "app-password"),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "not_found")
        self.assertEqual(len(result.messages), 1)
        recent = result.messages[0]
        self.assertIsInstance(recent, MailMessage)
        self.assertEqual(len(recent.body), 200_000)
        self.assertTrue(recent.body.startswith(private_prefix))
        self.assertNotIn("END-OF-RECENT-BODY", recent.body)
        self.assertNotIn(private_prefix, repr(recent))
        self.assertNotIn(private_prefix, repr(result))

    def test_fetches_html_as_plain_text_without_tags_scripts_or_styles(self) -> None:
        html = (
            "<html><head><style>.secret { color: red; }</style></head><body>"
            "<h1>Security notice</h1><p>Your verification code is "
            "<strong>246810</strong>.</p><script>stealBody()</script></body></html>"
        )
        _FakeIMAP.messages = {
            b"1": message_bytes("Your verification code", html, html=True),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@zoho.com", "app-password"),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "246810")
        self.assertIn("Your verification code is 246810.", result.body)
        self.assertNotIn("<strong>", result.body)
        self.assertNotIn("<script>", result.body)
        self.assertNotIn("stealBody", result.body)
        self.assertNotIn(".secret", result.body)

    def test_fetched_body_is_capped_at_200000_characters(self) -> None:
        prefix = "Your verification code is 135790.\n"
        oversized_body = prefix + ("x" * 205_000) + "END-OF-MESSAGE"
        _FakeIMAP.messages = {
            b"1": message_bytes("Security code", oversized_body),
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@yandex.com", "app-password"),
                max_age_hours=0,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "135790")
        self.assertEqual(len(result.body), 200_000)
        self.assertTrue(result.body.startswith("Your verification code is 135790."))
        self.assertNotIn("END-OF-MESSAGE", result.body)

    def test_auth_error_is_sanitized(self) -> None:
        _FakeIMAP.reject_all = True
        account = Account("person@yandex.com", "never-print-this")
        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account)

        self.assertEqual(result.status, "auth_error")
        self.assertNotIn(account.password, repr(result))

    def test_zoho_uses_regional_host_and_full_email_login_only(self) -> None:
        _FakeIMAP.messages = {
            b"1": message_bytes("Your verification code", "Code: 483920"),
        }
        account = Account("person@zoho.eu", "zoho-app-secret")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account, max_age_hours=0)

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "483920")
        self.assertEqual([client.host for client in _FakeIMAP.instances], ["imap.zoho.eu"])
        self.assertEqual(_FakeIMAP.login_names, [account.email])
        self.assertEqual(_FakeIMAP.auth_mechanisms, [])

    def test_explicit_zoho_preset_supports_custom_domain(self) -> None:
        _FakeIMAP.messages = {
            b"1": message_bytes("Login code", "Your login code is 592741"),
        }
        host = ZOHO_IMAP_PRESETS["India Organization"]
        account = Account(
            "person@company.example",
            "zoho-app-secret",
            provider="zoho",
            imap_host=host,
        )

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account, max_age_hours=0)

        self.assertEqual(result.status, "found")
        self.assertEqual([client.host for client in _FakeIMAP.instances], [host])
        self.assertEqual(_FakeIMAP.login_names, [account.email])

    def test_unknown_provider_and_unapproved_host_never_connect(self) -> None:
        accounts = (
            Account("person@example.com", "do-not-send"),
            Account(
                "person@company.example",
                "do-not-send",
                provider="zoho",
                imap_host="imap.attacker.example",
            ),
        )
        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            results = [fetch_latest_code(account) for account in accounts]

        self.assertEqual([result.status for result in results], ["unsupported_provider"] * 2)
        self.assertEqual(_FakeIMAP.instances, [])
        for account, result in zip(accounts, results):
            self.assertNotIn(account.password, result.detail)
            if account.imap_host:
                self.assertNotIn(account.imap_host, result.detail)

    def test_microsoft_without_token_never_connects_or_uses_password_login(self) -> None:
        account = Account("person@hotmail.com", "password-must-not-be-used")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account)

        self.assertEqual(result.status, "oauth_required")
        self.assertEqual(_FakeIMAP.instances, [])
        self.assertEqual(_FakeIMAP.login_names, [])
        self.assertNotIn(account.password, result.detail)

    def test_microsoft_uses_xoauth2_token_and_never_password_login(self) -> None:
        _FakeIMAP.messages = {
            b"1": message_bytes("Security code", "Your verification code is 731904"),
        }
        token = "oauth-token-never-return"
        account = Account("person@outlook.com", "password-must-not-be-used")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                account,
                max_age_hours=0,
                access_token_provider=lambda supplied: token if supplied is account else None,
            )

        self.assertEqual(result.status, "found")
        self.assertEqual(result.code, "731904")
        self.assertEqual([client.host for client in _FakeIMAP.instances], ["outlook.office365.com"])
        self.assertEqual(_FakeIMAP.login_names, [])
        self.assertEqual(_FakeIMAP.auth_mechanisms, ["XOAUTH2"])
        self.assertEqual(
            _FakeIMAP.auth_payloads,
            [f"user={account.email}\x01auth=Bearer {token}\x01\x01".encode("utf-8")],
        )
        self.assertNotIn(account.password, repr(result))
        self.assertNotIn(token, repr(result))

    def test_microsoft_token_callback_failure_is_sanitized_and_does_not_connect(self) -> None:
        diagnostic = "callback-internal-secret"

        def fail_safely(_account: Account) -> str:
            raise RuntimeError(diagnostic)

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(
                Account("person@live.com", "unused-password"),
                access_token_provider=fail_safely,
            )

        self.assertEqual(result.status, "oauth_required")
        self.assertEqual(_FakeIMAP.instances, [])
        self.assertNotIn(diagnostic, result.detail)

    def test_microsoft_oauth_rejection_is_sanitized(self) -> None:
        _FakeIMAP.reject_all = True
        token = "rejected-oauth-token"
        account = Account("person@msn.com", "unused-password")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeIMAP):
            result = fetch_latest_code(account, access_token_provider=lambda _account: token)

        self.assertEqual(result.status, "auth_error")
        self.assertEqual(_FakeIMAP.login_names, [])
        self.assertEqual(_FakeIMAP.auth_mechanisms, ["XOAUTH2"])
        self.assertNotIn(token, result.detail)
        self.assertNotIn(account.password, result.detail)

    def test_connection_error_is_structured(self) -> None:
        def cannot_connect(*args, **kwargs):
            raise OSError("network unavailable; no credentials here")

        with patch("yandex_mail.imaplib.IMAP4_SSL", cannot_connect):
            result = fetch_latest_code(Account("person@yandex.com", "secret"))
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(result.code, "")


class DeleteAllMailTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeDeleteIMAP.reset()

    def test_permanently_deletes_every_selectable_real_folder(self) -> None:
        account = Account("person@yandex.com", "never-log-this")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(account)

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "deleted")
        self.assertEqual(result.deleted_count, 12)
        self.assertEqual(result.folder_count, 7)
        self.assertEqual(_FakeDeleteIMAP.login_names, ["person", "person@yandex.com"])
        self.assertEqual(
            [client._identity(mailbox) for mailbox, _ in client.select_calls],
            [
                b"INBOX",
                b"Subscriptions",
                b"Social",
                b"My folders|Archive",
                b"Sent",
                b"Drafts",
                b"Spam",
                b"Trash",
            ],
        )
        self.assertTrue(all(not readonly for _, readonly in client.select_calls))
        self.assertNotIn(b"With attachments", [call[0] for call in client.uid_calls])
        self.assertNotIn(b"My folders", [call[0] for call in client.uid_calls])
        self.assertEqual(
            [call for call in client.uid_calls if call[1] == "STORE"],
            [
                (b"INBOX", "STORE", "1:4", "+FLAGS.SILENT", r"(\Deleted)"),
                (
                    b"Subscriptions",
                    "STORE",
                    "1:9",
                    "+FLAGS.SILENT",
                    r"(\Deleted)",
                ),
                (b"Social", "STORE", "1:8", "+FLAGS.SILENT", r"(\Deleted)"),
                (
                    b"My folders|Archive",
                    "STORE",
                    "1:7",
                    "+FLAGS.SILENT",
                    r"(\Deleted)",
                ),
                (b"Sent", "STORE", "1:10", "+FLAGS.SILENT", r"(\Deleted)"),
                (b"Spam", "STORE", "1:12", "+FLAGS.SILENT", r"(\Deleted)"),
                (b"Trash", "STORE", "1:22", "+FLAGS.SILENT", r"(\Deleted)"),
            ],
        )
        self.assertTrue(
            all(not uids for uids in _FakeDeleteIMAP.uids_by_mailbox.values())
        )
        self.assertTrue(all(instance.logged_out for instance in _FakeDeleteIMAP.instances))
        self.assertNotIn(account.password, result.detail)

    def test_empty_mailbox_inspects_all_real_folders_without_mutation(self) -> None:
        _FakeDeleteIMAP.uids_by_mailbox = {
            mailbox: b"" for mailbox in _FakeDeleteIMAP.uids_by_mailbox
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.deleted_count, 0)
        self.assertEqual(result.folder_count, 0)
        self.assertEqual(client.list_calls, 1)
        self.assertEqual(len(client.select_calls), 8)
        self.assertTrue(all(call[1] == "SEARCH" for call in client.uid_calls))
        self.assertTrue(client.logged_out)

    def test_uid_snapshot_does_not_delete_mail_delivered_after_search(self) -> None:
        _FakeDeleteIMAP.uids_by_mailbox = {
            mailbox: (b"1 4" if mailbox == b"INBOX" else b"")
            for mailbox in _FakeDeleteIMAP.uids_by_mailbox
        }
        _FakeDeleteIMAP.inject_uid_after_search = {b"INBOX": 100}

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "deleted")
        self.assertEqual(result.deleted_count, 2)
        self.assertEqual(result.folder_count, 1)
        self.assertEqual(_FakeDeleteIMAP.uids_by_mailbox[b"INBOX"], b"100")
        self.assertIn(
            (b"INBOX", "STORE", "1:4", "+FLAGS.SILENT", r"(\Deleted)"),
            client.uid_calls,
        )

    def test_server_without_uidplus_fails_before_listing_or_mutation(self) -> None:
        _FakeDeleteIMAP.advertised_capabilities = b"IMAP4rev1"

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.select_calls, [])
        self.assertEqual(client.uid_calls, [])
        self.assertTrue(client.logged_out)

    def test_store_failure_is_unknown_and_sanitized(self) -> None:
        _FakeDeleteIMAP.fail_at = (b"INBOX", "STORE")
        account = Account("person@yandex.com", "never-log-this")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(account)

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "outcome_unknown")
        self.assertEqual(
            client.uid_calls,
            [
                (b"INBOX", "SEARCH", None, "ALL"),
                (b"INBOX", "STORE", "1:4", "+FLAGS.SILENT", r"(\Deleted)"),
            ],
        )
        self.assertIn("Check every folder", result.detail)
        self.assertNotIn(account.password, result.detail)
        self.assertNotIn("server-only", result.detail)

    def test_unexpected_failure_after_store_is_unknown_and_sanitized(self) -> None:
        _FakeDeleteIMAP.raise_at = (b"INBOX", "EXPUNGE")
        account = Account("person@yandex.com", "never-log-this")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(account)

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "outcome_unknown")
        self.assertEqual(result.deleted_count, 0)
        self.assertTrue(client.logged_out)
        self.assertNotIn(account.password, result.detail)
        self.assertNotIn("server-only", result.detail)

    def test_later_folder_failure_reports_confirmed_partial_count(self) -> None:
        _FakeDeleteIMAP.fail_at = (b"Subscriptions", "SELECT")
        account = Account("person@yandex.com", "never-log-this")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(account)

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.deleted_count, 2)
        self.assertEqual(result.folder_count, 1)
        self.assertEqual(client._identity(client.select_calls[-1][0]), b"Subscriptions")
        self.assertNotIn(account.password, result.detail)
        self.assertNotIn("server-only", result.detail)

    def test_malformed_list_fails_closed_before_selecting_a_folder(self) -> None:
        _FakeDeleteIMAP.folder_data = [
            b'(\\HasNoChildren) "|" "INBOX"',
            b"not a valid LIST response",
        ]

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(client.select_calls, [])
        self.assertEqual(client.uid_calls, [])

    def test_malformed_quoted_mailbox_fails_closed_before_mutation(self) -> None:
        _FakeDeleteIMAP.folder_data = [
            b'(\\HasNoChildren) "|" "INBOX"',
            b'(\\HasNoChildren) "|" "Broken',
            b'(\\HasNoChildren \\Trash) "|" "Trash"',
        ]

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(client.select_calls, [])
        self.assertEqual(client.uid_calls, [])

    def test_mismatched_literal_length_fails_closed_before_mutation(self) -> None:
        _FakeDeleteIMAP.folder_data = [
            b'(\\HasNoChildren) "|" "INBOX"',
            (b'(\\HasNoChildren) "|" {99}', b"Custom Name"),
            b'(\\HasNoChildren \\Trash) "|" "Trash"',
        ]

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(client.select_calls, [])
        self.assertEqual(client.uid_calls, [])

    def test_missing_inbox_fails_closed_before_mutation(self) -> None:
        _FakeDeleteIMAP.folder_data = [b'(\\HasNoChildren \\Sent) "|" "Sent"']

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(client.select_calls, [])
        self.assertEqual(client.uid_calls, [])

    def test_missing_trash_fails_closed_before_mutation(self) -> None:
        _FakeDeleteIMAP.folder_data = [b'(\\HasNoChildren) "|" "INBOX"']

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(client.select_calls, [])
        self.assertEqual(client.uid_calls, [])

    def test_literal_mailbox_name_is_safely_quoted_and_purged(self) -> None:
        _FakeDeleteIMAP.folder_data = [
            b'(\\HasNoChildren) "|" "INBOX"',
            (b'(\\HasNoChildren) "|" {11}', b"Custom Name"),
            b'(\\HasNoChildren \\Trash) "|" "Trash"',
        ]
        _FakeDeleteIMAP.uids_by_mailbox = {
            b"INBOX": b"",
            b"Custom Name": b"41",
            b"Trash": b"",
        }

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "deleted")
        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(result.folder_count, 1)
        self.assertIn((b'"Custom Name"', False), client.select_calls)
        self.assertEqual(_FakeDeleteIMAP.uids_by_mailbox[b"Custom Name"], b"")

    def test_malformed_uid_search_fails_before_any_mutation(self) -> None:
        _FakeDeleteIMAP.uids_by_mailbox[b"INBOX"] = b"1 invalid"

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(
            client.uid_calls,
            [(b"INBOX", "SEARCH", None, "ALL")],
        )

    def test_missing_initial_search_data_is_not_treated_as_empty(self) -> None:
        _FakeDeleteIMAP.invalid_search_at = (b"INBOX", "INITIAL")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "connection_error")
        self.assertEqual(
            client.uid_calls,
            [(b"INBOX", "SEARCH", None, "ALL")],
        )

    def test_missing_verification_data_is_an_unknown_outcome(self) -> None:
        _FakeDeleteIMAP.invalid_search_at = (b"INBOX", "VERIFY")

        with patch("yandex_mail.imaplib.IMAP4_SSL", _FakeDeleteIMAP):
            result = delete_all_yandex_mail(
                Account("person@yandex.com", "app-password")
            )

        client = _FakeDeleteIMAP.instances[-1]
        self.assertEqual(result.status, "outcome_unknown")
        self.assertEqual(result.deleted_count, 0)
        self.assertTrue(client.logged_out)

    def test_non_yandex_account_is_rejected_before_connecting(self) -> None:
        with patch(
            "yandex_mail.imaplib.IMAP4_SSL",
            side_effect=AssertionError("must not connect"),
        ):
            result = delete_all_yandex_mail(
                Account("person@zoho.com", "do-not-send", "zoho")
            )

        self.assertEqual(result.status, "unsupported_provider")
        self.assertIn("Yandex", result.detail)


if __name__ == "__main__":
    unittest.main()
