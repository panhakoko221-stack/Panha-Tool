"""Small, UI-agnostic IMAP client for codes and Yandex mailbox cleanup.

The public API deliberately returns only sanitized values.  Passwords live on
``Account`` long enough to authenticate, but are never copied to a result or
an error message.  Microsoft access tokens are supplied just in time through
``fetch_latest_code`` and are likewise never returned or logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import imaplib
import re
from typing import Callable, Iterable, Optional


IMAP_HOST = "imap.yandex.com"
IMAP_PORT = 993
_MAX_RESULT_BODY_CHARS = 200_000


@dataclass(frozen=True)
class ProviderSpec:
    """Connection policy for a supported mail provider."""

    key: str
    label: str
    imap_host: str
    imap_port: int = IMAP_PORT
    auth_method: str = "password"
    login_mode: str = "full_email"


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "yandex": ProviderSpec(
        key="yandex",
        label="Yandex Mail",
        imap_host=IMAP_HOST,
        auth_method="password",
        login_mode="local_then_full",
    ),
    "zoho": ProviderSpec(
        key="zoho",
        label="Zoho Mail",
        imap_host="imap.zoho.com",
        auth_method="password",
        login_mode="full_email",
    ),
    "microsoft": ProviderSpec(
        key="microsoft",
        label="Outlook / Hotmail",
        imap_host="outlook.office365.com",
        auth_method="oauth2",
        login_mode="full_email",
    ),
}


# Zoho assigns accounts to regional data centres.  Only documented Zoho
# endpoints are accepted as overrides so a pasted account cannot silently send
# a password to an arbitrary host.
ZOHO_IMAP_PRESETS: dict[str, str] = {
    "Global Personal": "imap.zoho.com",
    "US Organization": "imappro.zoho.com",
    "Europe Personal": "imap.zoho.eu",
    "Europe Organization": "imappro.zoho.eu",
    "India Personal": "imap.zoho.in",
    "India Organization": "imappro.zoho.in",
    "Australia": "imap.zoho.com.au",
    "Japan": "imap.zoho.jp",
    "China": "imap.zoho.com.cn",
}
_ZOHO_ALLOWED_HOSTS = frozenset(ZOHO_IMAP_PRESETS.values())

_YANDEX_DOMAINS = frozenset(
    {"yandex.com", "yandex.ru", "yandex.by", "yandex.kz", "yandex.uz", "ya.ru"}
)
_ZOHO_DOMAIN_HOSTS = {
    "zoho.com": "imap.zoho.com",
    "zohomail.com": "imap.zoho.com",
    "zoho.eu": "imap.zoho.eu",
    "zohomail.eu": "imap.zoho.eu",
    "zoho.in": "imap.zoho.in",
    "zohomail.in": "imap.zoho.in",
    "zoho.com.au": "imap.zoho.com.au",
    "zohomail.com.au": "imap.zoho.com.au",
    "zoho.jp": "imap.zoho.jp",
    "zohomail.jp": "imap.zoho.jp",
    "zoho.com.cn": "imap.zoho.com.cn",
    "zohomail.com.cn": "imap.zoho.com.cn",
}
_MICROSOFT_DOMAINS = frozenset({"outlook.com", "hotmail.com", "live.com", "msn.com"})
_PROVIDER_ALIASES = {
    "outlook": "microsoft",
    "hotmail": "microsoft",
    "outlook_hotmail": "microsoft",
    "yandex_mail": "yandex",
    "zoho_mail": "zoho",
}


def _email_domain(email_address: str) -> str:
    address = str(email_address or "").strip()
    if address.count("@") != 1 or "|" in address or any(char.isspace() for char in address):
        return ""
    local_part, domain = address.split("@", 1)
    if not local_part or not domain or "." not in domain:
        return ""
    return domain.casefold()


def detect_provider(email_address: str) -> str:
    """Return a canonical provider key using exact, case-insensitive domains."""

    domain = _email_domain(email_address)
    if domain in _YANDEX_DOMAINS:
        return "yandex"
    if domain in _ZOHO_DOMAIN_HOSTS:
        return "zoho"
    if domain in _MICROSOFT_DOMAINS:
        return "microsoft"
    return "unsupported"


def _canonical_provider(provider: object) -> str:
    key = str(provider or "auto").strip().casefold().replace("-", "_")
    return _PROVIDER_ALIASES.get(key, key)


def provider_label(provider: str) -> str:
    """Return a safe display label for a provider key."""

    key = _canonical_provider(provider)
    if key == "auto":
        return "Auto detect"
    if key == "unsupported":
        return "Unsupported provider"
    spec = PROVIDER_SPECS.get(key)
    return spec.label if spec is not None else "Unsupported provider"


@dataclass(frozen=True, repr=False)
class Account:
    """A mail login and its password or app password.

    ``repr(account)`` is safe to put in a debug log; the actual password is
    intentionally redacted.
    """

    email: str
    password: str
    provider: str = "auto"
    imap_host: str = ""
    mail_alias: str = ""

    def __repr__(self) -> str:
        alias = f", mail_alias={self.mail_alias!r}" if self.mail_alias else ""
        return (
            f"Account(email={self.email!r}, password=<redacted>, "
            f"provider={self.provider!r}, imap_host={self.imap_host!r}{alias})"
        )


@dataclass(frozen=True)
class MailMessage:
    """Sanitized message metadata and decoded body for the All Mail list."""

    code: str = ""
    subject: str = ""
    sender: str = ""
    received: str = ""
    body: str = field(default="", repr=False)
    recipient: str = ""
    facebook_name: str = ""
    received_timestamp: float = field(default=0.0, repr=False)


@dataclass(frozen=True)
class FetchResult:
    """Sanitized result returned to the UI."""

    status: str
    code: str = ""
    subject: str = ""
    sender: str = ""
    received: str = ""
    detail: str = ""
    body: str = field(default="", repr=False)
    messages: tuple[MailMessage, ...] = field(default_factory=tuple, repr=False)
    recipient: str = ""
    facebook_name: str = ""


@dataclass(frozen=True)
class DeleteMailResult:
    """Sanitized outcome from permanently deleting all Yandex mailbox mail."""

    status: str
    deleted_count: int = 0
    detail: str = ""
    folder_count: int = 0


_EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")


def parse_account_lines(text: str) -> tuple[list[Account], list[str]]:
    """Parse legacy and explicitly provider-prefixed account lines.

    ``email|password`` keeps automatic provider detection.  A recognized
    ``provider|email|password`` prefix selects Yandex or Zoho explicitly,
    which is useful for provider-hosted custom domains.  Splitting is limited
    so passwords can contain ``|`` in both formats.  Microsoft password lines
    are rejected because Microsoft IMAP requires OAuth.

    Blank lines are ignored.  Errors contain a line number and a reason, but
    never echo the source line because that line can contain a password.
    Email de-duplication is case-insensitive.
    """

    accounts: list[Account] = []
    errors: list[str] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if "|" not in line:
            errors.append(f"Line {line_number}: expected email|password.")
            continue

        parts = [part.strip() for part in line.split("|")]
        first_field = parts[0]
        explicit_provider = _canonical_provider(first_field)
        has_provider_prefix = (
            "@" not in first_field
            and explicit_provider in {"auto", "yandex", "zoho", "microsoft"}
        )
        mail_alias = ""
        if has_provider_prefix:
            fields = line.split("|", 2)
            if len(fields) != 3:
                errors.append(
                    f"Line {line_number}: expected provider|email|password."
                )
                continue
            _, email_address, rem = (part.strip() for part in fields)
            provider = explicit_provider
            if "|" in rem:
                p_parts = rem.rsplit("|", 1)
                if "@" in p_parts[1]:
                    password = p_parts[0].strip()
                    mail_alias = p_parts[1].strip()
                else:
                    password = rem
            else:
                password = rem
        else:
            email_address, rem = (part.strip() for part in line.split("|", 1))
            provider = "auto"
            if "|" in rem:
                p_parts = rem.rsplit("|", 1)
                if "@" in p_parts[1]:
                    password = p_parts[0].strip()
                    mail_alias = p_parts[1].strip()
                else:
                    password = rem
            else:
                password = rem

        if not email_address:
            errors.append(f"Line {line_number}: email is empty.")
            continue
        if not password:
            errors.append(f"Line {line_number}: password is empty.")
            continue
        if not _EMAIL_RE.fullmatch(email_address):
            errors.append(f"Line {line_number}: email address is invalid.")
            continue

        resolved_provider = detect_provider(email_address) if provider == "auto" else provider
        if resolved_provider == "microsoft":
            errors.append(
                f"Line {line_number}: Microsoft accounts require OAuth and cannot import a password."
            )
            continue

        key = email_address.casefold()
        if key in seen:
            continue
        seen.add(key)
        accounts.append(
            Account(
                email_address,
                password,
                provider=provider,
                mail_alias=mail_alias,
            )
        )

    return accounts, errors


def _decode_bytes(value: bytes, charset: Optional[str] = None) -> str:
    encodings = [charset, "utf-8", "windows-1251", "latin-1"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return value.decode("utf-8", errors="replace")


def decode_mime_header(value: object) -> str:
    """Decode an RFC 2047 header without failing on broken charsets."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)

    decoded: list[str] = []
    try:
        fragments = decode_header(value)
    except (TypeError, ValueError):
        return value
    for fragment, charset in fragments:
        if isinstance(fragment, bytes):
            decoded.append(_decode_bytes(fragment, charset))
        else:
            decoded.append(fragment)
    return "".join(decoded).strip()


class _TextHTMLParser(HTMLParser):
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
        "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "head"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "head"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = unescape("".join(self.parts)).replace("\r", "")
        value = re.sub(r"[\t\f\v ]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def strip_html(html: str) -> str:
    """Convert message HTML to readable text while dropping scripts/styles."""

    parser = _TextHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        return parser.text()
    except (AssertionError, ValueError):
        # A conservative fallback for unusually malformed email HTML.
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", html))).strip()


def _decode_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, bytes):
        return _decode_bytes(payload, part.get_content_charset())
    raw = part.get_payload()
    return raw if isinstance(raw, str) else ""


def decode_message_body(message: Message) -> str:
    """Return decoded plain/HTML message text, ignoring attachments."""

    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts: Iterable[Message] = message.walk() if message.is_multipart() else (message,)

    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").casefold()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type == "text/plain":
            plain_parts.append(_decode_part(part))
        elif content_type == "text/html":
            html_parts.append(strip_html(_decode_part(part)))

    # Keep both alternatives: some automated messages put a generic sentence
    # in text/plain but render the actual code only in text/html.  Exact
    # duplicates are removed to keep the extraction context tidy.
    chosen: list[str] = []
    seen: set[str] = set()
    for candidate in plain_parts + html_parts:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            chosen.append(candidate)
    return "\n\n".join(chosen).strip()


_KEYWORD_RE = re.compile(
    r"(?:"
    r"(?:verification|confirmation|security|login|sign[- ]?in|authentication|"
    r"authorization|access|temporary|one[- ]?time)\s+(?:code|pin|password)"
    r"|(?:code|pin)\s+(?:for\s+)?(?:verification|confirmation|login|sign[- ]?in)"
    r"|(?:code|pin)\s+to\s+(?:confirm|verify)\b"
    r"|passcode|\botp\b"
    r"|код\s+(?:подтверждения|проверки|доступа|авторизации)"
    r"|проверочный\s+код"
    r")",
    re.IGNORECASE,
)

_FACEBOOK_CONTEXT_RE = re.compile(r"facebook", re.IGNORECASE)
_FACEBOOK_GENERIC_LINE_RE = re.compile(
    r"^(?:facebook|meta|your\b|you\b|we\b|this\b|the\b|please\b|confirm\b|"
    r"verification\b|confirmation\b|security\b|code\b|thanks?\b|regards?\b|"
    r"copyright\b|https?://|www\.)",
    re.IGNORECASE,
)
_FACEBOOK_NAME_TRAILER_RE = re.compile(
    r"^(.{1,80}?)[,，]\s*(?:confirm\b|one\s+more\b|please\b|you\b|we\b)",
    re.IGNORECASE,
)
_SEPARATOR = r"[ \t\u00a0\u2007\u2009-]{0,3}"
_DIGIT_SEQUENCE_RE = re.compile(
    rf"(?<!\d)"
    rf"(\d(?:{_SEPARATOR}\d){{3,}})"
    rf"(?!\d)"
)
_DATE_RE = re.compile(
    r"^(?:19|20)\d{2}[- /.](?:0?[1-9]|1[0-2])[- /.](?:0?[1-9]|[12]\d|3[01])$"
    r"|^(?:0?[1-9]|[12]\d|3[01])[- /.](?:0?[1-9]|1[0-2])[- /.](?:19|20)\d{2}$"
)
_UNRELATED_RE = re.compile(
    r"\b(?:order|invoice|receipt|reference|tracking|shipment|ticket|case|customer|"
    r"account|phone|date|year|total)\s*(?:number|no\.?|id|#|:)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _CodeCandidate:
    code: str
    start: int
    end: int
    explicit: bool
    distance: int


def _looks_like_date(raw: str, digits: str) -> bool:
    if _DATE_RE.fullmatch(raw):
        return True
    if len(digits) != 8:
        return False
    # Compact dates occur frequently in automated subjects and order mail.
    year_first = 1900 <= int(digits[:4]) <= 2099
    valid_month_day = 1 <= int(digits[4:6]) <= 12 and 1 <= int(digits[6:]) <= 31
    year_last = 1900 <= int(digits[4:]) <= 2099
    valid_day_month = 1 <= int(digits[:2]) <= 31 and 1 <= int(digits[2:4]) <= 12
    return (year_first and valid_month_day) or (year_last and valid_day_month)


def _code_candidates(text: str) -> list[_CodeCandidate]:
    keywords = list(_KEYWORD_RE.finditer(text))
    candidates: list[_CodeCandidate] = []
    for match in _DIGIT_SEQUENCE_RE.finditer(text):
        raw = match.group(1).strip()
        code = "".join(re.findall(r"\d", raw))
        if not 4 <= len(code) <= 8:
            continue
        if _looks_like_date(raw, code):
            continue

        distances: list[int] = []
        for keyword in keywords:
            if keyword.end() <= match.start():
                distances.append(match.start() - keyword.end())
            elif match.end() <= keyword.start():
                distances.append(keyword.start() - match.end())
            else:
                distances.append(0)
        distance = min(distances, default=10_000)
        explicit = distance <= 64

        before = text[max(0, match.start() - 45):match.start()]
        if _UNRELATED_RE.search(before):
            continue
        if not explicit and len(code) == 4 and 1900 <= int(code) <= 2099:
            continue
        candidates.append(_CodeCandidate(code, match.start(), match.end(), explicit, distance))
    return candidates


def extract_verification_code(subject: str, body: str) -> Optional[str]:
    """Extract a likely 4-8 digit login code.

    Explicit verification wording wins.  Spaced codes such as ``1 2 3 4 5 6``
    are normalized.  A bare number is accepted only when the subject or body
    establishes that the message is about a verification code; this prevents
    dates and common order/reference numbers from being returned.
    """

    subject = subject or ""
    body = body or ""
    subject_candidates = _code_candidates(subject)
    body_candidates = _code_candidates(body)

    ranked: list[tuple[int, int, int, _CodeCandidate]] = []
    for location_rank, candidates in enumerate((subject_candidates, body_candidates)):
        for candidate in candidates:
            if candidate.explicit:
                ranked.append((0, candidate.distance, location_rank, candidate))
    if ranked:
        return min(ranked, key=lambda item: (item[0], item[1], item[2], item[3].start))[3].code

    has_verification_context = bool(_KEYWORD_RE.search(subject) or _KEYWORD_RE.search(body))
    if not has_verification_context:
        return None

    # A common template puts "Verification code" in the subject and displays
    # only a large bare number in the HTML body.
    candidates = subject_candidates + body_candidates
    if candidates:
        return candidates[0].code
    return None


def extract_facebook_name(subject: str, sender: str, body: str) -> str:
    """Return a conservative Facebook profile-name hint from message text.

    Facebook confirmation mail commonly starts with either ``Hi Name,`` or
    ``Name, confirm ...``.  The result is only presentation metadata; code
    detection never depends on it.
    """

    subject = str(subject or "")
    sender = str(sender or "")
    body = str(body or "")
    if not (
        _FACEBOOK_CONTEXT_RE.search(sender)
        or _FACEBOOK_CONTEXT_RE.search(subject)
        or re.search(r"\bconfirm\s+this\s+email\b", subject, re.IGNORECASE)
    ):
        return ""

    for raw_line in body.splitlines()[:12]:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or len(line) > 140 or "@" in line or re.search(r"https?://", line, re.I):
            continue
        line = re.sub(r"^(?:hi|hello|dear)\s+", "", line, flags=re.IGNORECASE).strip()
        trailer = _FACEBOOK_NAME_TRAILER_RE.match(line)
        candidate = trailer.group(1).strip() if trailer else line.rstrip(" ,，:;.!?")
        if (
            not candidate
            or len(candidate) > 80
            or _FACEBOOK_GENERIC_LINE_RE.search(candidate)
            or any(character.isdigit() for character in candidate)
            or not any(character.isalpha() for character in candidate)
            or len(candidate.split()) > 8
        ):
            continue
        return candidate
    return ""


def _parse_message(raw_message: bytes) -> tuple[str, str, str, str, Optional[datetime]]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = decode_mime_header(message.get("Subject"))
    sender = decode_mime_header(message.get("From"))
    received = decode_mime_header(message.get("Date"))
    body = decode_message_body(message)
    received_at: Optional[datetime] = None
    if received:
        try:
            received_at = parsedate_to_datetime(received)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
            received_at = received_at.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return subject, sender, received, body, received_at


_RECIPIENT_HEADERS = (
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "X-Envelope-To",
    "To",
    "Cc",
    "Apparently-To",
    "Resent-To",
)


def _message_recipient_addresses(raw_message: bytes) -> tuple[str, ...]:
    """Return decoded recipient addresses without surfacing parser failures."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        header_values: list[str] = []
        for header_name in _RECIPIENT_HEADERS:
            for value in message.get_all(header_name, []):
                decoded = decode_mime_header(value)
                if decoded:
                    header_values.append(decoded)
        parsed_addresses = getaddresses(header_values)
    except Exception:
        return ()

    addresses: list[str] = []
    seen: set[str] = set()
    for _, address in parsed_addresses:
        clean = address.strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            addresses.append(clean)
    return tuple(addresses)


def _preferred_recipient_address(addresses: Iterable[str], login_email: str) -> str:
    """Prefer a plus-address belonging to the login mailbox for display."""

    login_local, separator, login_domain = str(login_email or "").strip().rpartition("@")
    login_base = login_local.split("+", 1)[0].casefold()
    matching: list[str] = []
    fallback: list[str] = []
    for address in addresses:
        clean = str(address or "").strip()
        if not clean:
            continue
        fallback.append(clean)
        local, address_separator, domain = clean.rpartition("@")
        if (
            separator
            and address_separator
            and domain.casefold() == login_domain.casefold()
            and local.split("+", 1)[0].casefold() == login_base
        ):
            matching.append(clean)
    return next((address for address in matching if "+" in address.rpartition("@")[0]), matching[0] if matching else (fallback[0] if fallback else ""))


def _message_is_addressed_to(raw_message: bytes, mail_alias: str) -> bool:
    """Return whether a decoded recipient header exactly names ``mail_alias``."""

    target = str(mail_alias or "").strip().casefold()
    if not target:
        return True

    return any(
        address.casefold() == target
        for address in _message_recipient_addresses(raw_message)
    )


def _login_candidates(
    email_address: str,
    provider: Optional[ProviderSpec] = None,
) -> list[str]:
    """Return provider-approved login names.

    The default remains Yandex's historical local-part/full-address fallback
    for compatibility with callers that exercised this private helper.
    """

    provider = provider or PROVIDER_SPECS["yandex"]
    if provider.login_mode == "local_then_full":
        local_part = email_address.split("@", 1)[0]
        return list(dict.fromkeys((local_part, email_address)))
    return [email_address]


def _safe_logout(client: object) -> None:
    try:
        client.logout()  # type: ignore[attr-defined]
    except Exception:
        pass


def _status_ok(status: object) -> bool:
    if isinstance(status, bytes):
        status = status.decode("ascii", errors="ignore")
    return str(status).upper() == "OK"


def _resolve_provider(
    account: Account,
) -> tuple[Optional[ProviderSpec], str, str]:
    """Return (provider, host, safe_error_detail) for an account."""

    domain = _email_domain(account.email)
    if not domain:
        return None, "", "This email provider is not supported."
    requested = _canonical_provider(account.provider)
    provider_key = detect_provider(account.email) if requested == "auto" else requested
    provider = PROVIDER_SPECS.get(provider_key)
    if provider is None:
        return None, "", "This email provider is not supported."

    requested_host = str(account.imap_host or "").strip().casefold()
    if requested_host:
        if provider.key != "zoho" or requested_host not in _ZOHO_ALLOWED_HOSTS:
            return None, "", "The selected IMAP server preset is not supported."
        host = requested_host
    elif provider.key == "zoho":
        host = _ZOHO_DOMAIN_HOSTS.get(domain, provider.imap_host)
    else:
        host = provider.imap_host
    return provider, host, ""


_PROVIDER_DETAILS: dict[str, dict[str, str]] = {
    "yandex": {
        "auth_error": "Yandex rejected the account credentials.",
        "connect_error": "Could not connect to Yandex IMAP.",
        "inbox_error": "Could not open the Yandex inbox.",
        "list_error": "Could not list inbox messages.",
        "connection_failed": "The Yandex IMAP connection failed.",
        "folder_list_error": "Could not list every Yandex mail folder.",
        "folder_error": "Could not open or inspect every Yandex mail folder.",
        "delete_error": "Could not safely delete all Yandex messages.",
        "delete_partial": (
            "Some Yandex messages were permanently deleted, but the mailbox "
            "could not be fully emptied. Check every folder before trying again."
        ),
        "delete_unknown": (
            "Yandex could not confirm whether all messages were permanently deleted. "
            "Check every folder before trying again."
        ),
    },
    "zoho": {
        "auth_error": "Zoho Mail rejected the account credentials.",
        "connect_error": "Could not connect to Zoho Mail IMAP.",
        "inbox_error": "Could not open the Zoho Mail inbox.",
        "list_error": "Could not list inbox messages.",
        "connection_failed": "The Zoho Mail IMAP connection failed.",
    },
    "microsoft": {
        "auth_error": "Microsoft rejected the mail authorization.",
        "connect_error": "Could not connect to Outlook Mail IMAP.",
        "inbox_error": "Could not open the Outlook Mail inbox.",
        "list_error": "Could not list inbox messages.",
        "connection_failed": "The Outlook Mail IMAP connection failed.",
        "oauth_required": "Connect this Microsoft account with OAuth before fetching mail.",
    },
}


def _provider_detail(provider: ProviderSpec, event: str) -> str:
    """Return only controlled, credential-free provider text."""

    return _PROVIDER_DETAILS.get(provider.key, {}).get(event, "The mail request failed.")


def _connect_and_login(
    account: Account,
    timeout: int,
    provider: Optional[ProviderSpec] = None,
    imap_host: str = "",
    access_token: str = "",
) -> tuple[Optional[imaplib.IMAP4_SSL], str]:
    """Return (client, error_status) using the provider's auth policy."""

    provider = provider or PROVIDER_SPECS["yandex"]
    host = imap_host or provider.imap_host
    if provider.auth_method == "oauth2" and not access_token:
        return None, "oauth_required"

    saw_auth_failure = False
    for login_name in _login_candidates(account.email, provider):
        try:
            client = imaplib.IMAP4_SSL(host, provider.imap_port, timeout=timeout)
        except (OSError, imaplib.IMAP4.error, TimeoutError):
            return None, "connection_error"
        try:
            if provider.auth_method == "oauth2":
                auth_payload = (
                    f"user={login_name}\x01auth=Bearer {access_token}\x01\x01"
                ).encode("utf-8")
                status, _ = client.authenticate("XOAUTH2", lambda _challenge: auth_payload)
            else:
                status, _ = client.login(login_name, account.password)
            if _status_ok(status):
                return client, ""
            saw_auth_failure = True
        except (imaplib.IMAP4.error, TypeError, UnicodeError):
            saw_auth_failure = True
        except (OSError, TimeoutError):
            _safe_logout(client)
            return None, "connection_error"
        _safe_logout(client)

    return None, "auth_error" if saw_auth_failure else "connection_error"


def _raw_message_from_fetch(data: object) -> Optional[bytes]:
    if not isinstance(data, (list, tuple)):
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
        if isinstance(item, bytes) and item.startswith((b"From:", b"Return-Path:")):
            return item
    return None


_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
_FETCH_SEQUENCE_RE = re.compile(rb"^\s*(\d+)\s")


def _metadata_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("ascii", errors="ignore")
    return b""


def _received_at_from_metadata(metadata: object) -> Optional[datetime]:
    value_bytes = _metadata_bytes(metadata)
    match = _INTERNALDATE_RE.search(value_bytes)
    if not match:
        return None
    try:
        value = match.group(1).decode("ascii")
        parsed = parsedate_to_datetime(value.replace("-", " ", 2))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None


def _message_id_from_metadata(metadata: object) -> bytes:
    match = _FETCH_SEQUENCE_RE.match(_metadata_bytes(metadata))
    return match.group(1) if match else b""


def _received_at_from_fetch(data: object) -> Optional[datetime]:
    """Return the server receipt timestamp from an IMAP FETCH response."""

    if not isinstance(data, (list, tuple)):
        return None
    for item in data:
        metadata = item[0] if isinstance(item, tuple) and item else item
        received_at = _received_at_from_metadata(metadata)
        if received_at is not None:
            return received_at
    return None


def _internal_dates_from_fetch(data: object) -> dict[bytes, datetime]:
    result: dict[bytes, datetime] = {}
    if not isinstance(data, (list, tuple)):
        return result
    for item in data:
        metadata = item[0] if isinstance(item, tuple) and item else item
        message_id = _message_id_from_metadata(metadata)
        received_at = _received_at_from_metadata(metadata)
        if message_id and received_at is not None:
            result[message_id] = received_at
    return result


def _message_records_from_fetch(
    data: object,
) -> list[tuple[bytes, bytes, Optional[datetime]]]:
    """Return sequence number, raw body, and INTERNALDATE for each literal."""

    records: list[tuple[bytes, bytes, Optional[datetime]]] = []
    if not isinstance(data, (list, tuple)):
        return records
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            metadata = item[0]
            records.append(
                (
                    _message_id_from_metadata(metadata),
                    item[1],
                    _received_at_from_metadata(metadata),
                )
            )
        elif isinstance(item, bytes) and item.startswith((b"From:", b"Return-Path:")):
            records.append((b"", item, None))
    return records


_LIST_MAILBOX_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?:NIL|"(?:\\.|[^"])*")\s+(?P<mailbox>.+)$',
    re.IGNORECASE,
)
_LIST_LITERAL_RE = re.compile(rb"^\{(?P<size>[0-9]+)\+?\}$")
_LIST_QUOTED_MAILBOX_RE = re.compile(
    rb'^"(?:[^"\\\x00\r\n]|\\["\\])+"$'
)
_LIST_ATOM_MAILBOX_RE = re.compile(
    rb'^[^\x00-\x20\x7f(){}%*"\\\]]+$'
)


_UNSELECTABLE_MAILBOX_FLAGS = frozenset({b"\\noselect", b"\\nonexistent"})
_AGGREGATE_MAILBOX_FLAGS = frozenset(
    {b"\\all", b"\\flagged", b"\\important", b"\\starred"}
)


def _quote_mailbox_literal(value: bytes) -> Optional[bytes]:
    if not value or any(character in value for character in (b"\x00", b"\r", b"\n")):
        return None
    escaped = value.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + escaped + b'"'


def _mailbox_identity(mailbox: bytes) -> bytes:
    value = mailbox.strip()
    if len(value) >= 2 and value.startswith(b'"') and value.endswith(b'"'):
        value = re.sub(rb"\\(.)", rb"\1", value[1:-1])
    return value.lower()


def _mailboxes_from_list(
    data: object,
) -> tuple[list[tuple[frozenset[bytes], bytes]], bool]:
    """Parse LIST data into exact selectable mailbox command tokens.

    The boolean reports whether every response item was parsed.  A mailbox-wide
    destructive action must fail closed instead of silently omitting a folder.
    """

    if not isinstance(data, (list, tuple)) or not data:
        return [], False

    mailboxes: list[tuple[frozenset[bytes], bytes]] = []
    seen: set[bytes] = set()
    complete = True
    for item in data:
        literal: Optional[bytes] = None
        if isinstance(item, bytes):
            line = item.strip()
        elif (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            line = item[0].strip()
            literal = item[1]
        else:
            complete = False
            continue

        match = _LIST_MAILBOX_RE.match(line)
        if match is None:
            complete = False
            continue
        flags = frozenset(flag.lower() for flag in match.group("flags").split())
        mailbox = match.group("mailbox").strip()
        if mailbox.startswith(b"{"):
            literal_match = _LIST_LITERAL_RE.fullmatch(mailbox)
            if (
                literal_match is None
                or literal is None
                or len(literal) != int(literal_match.group("size"))
            ):
                complete = False
                continue
            mailbox = _quote_mailbox_literal(literal) or b""
        elif literal is not None:
            complete = False
            continue
        elif mailbox.startswith(b'"'):
            if _LIST_QUOTED_MAILBOX_RE.fullmatch(mailbox) is None:
                complete = False
                continue
        elif _LIST_ATOM_MAILBOX_RE.fullmatch(mailbox) is None:
            complete = False
            continue
        if not mailbox or any(character in mailbox for character in (b"\x00", b"\r", b"\n")):
            complete = False
            continue
        if mailbox in seen:
            continue
        seen.add(mailbox)
        mailboxes.append((flags, mailbox))

    return mailboxes, complete


def _imap_capabilities(client: object) -> set[bytes]:
    """Return post-login capabilities without surfacing server diagnostics."""

    values: list[object] = []
    try:
        status, data = client.capability()  # type: ignore[attr-defined]
        if _status_ok(status) and isinstance(data, (list, tuple)):
            values.extend(data)
    except (AttributeError, OSError, imaplib.IMAP4.error, TimeoutError, TypeError):
        pass
    if not values:
        capabilities = getattr(client, "capabilities", ())
        if isinstance(capabilities, (list, tuple, set, frozenset)):
            values.extend(capabilities)

    result: set[bytes] = set()
    for value in values:
        if isinstance(value, str):
            value = value.encode("ascii", errors="ignore")
        if isinstance(value, bytes):
            result.update(part.upper() for part in value.split())
    return result


def _message_ids_from_search(data: object) -> Optional[list[bytes]]:
    if not isinstance(data, (list, tuple)) or not data:
        return None
    first = data[0]
    if isinstance(first, str):
        return first.encode("ascii", errors="ignore").split()
    if isinstance(first, bytes):
        return first.split()
    return None


def delete_all_yandex_mail(
    account: Account,
    timeout: int = 20,
) -> DeleteMailResult:
    """Permanently delete messages from every real Yandex mail folder.

    Folder structures are preserved.  ``\\Noselect`` containers and aggregate
    views such as ``\\All`` are skipped; their backing messages are removed from
    the selectable folders where they are actually stored.  Each folder uses a
    UID snapshot and targeted UID EXPUNGE, so later deliveries are not swept into
    a broad, unbounded deletion command.
    """

    timeout = max(1, int(timeout))
    provider, imap_host, provider_error = _resolve_provider(account)
    if provider is None:
        return DeleteMailResult("unsupported_provider", detail=provider_error)
    if provider.key != "yandex":
        return DeleteMailResult(
            "unsupported_provider",
            detail="Mailbox-wide deletion is available for Yandex accounts only.",
        )

    client, error_status = _connect_and_login(
        account,
        timeout,
        provider=provider,
        imap_host=imap_host,
    )
    if client is None:
        if error_status == "auth_error":
            return DeleteMailResult(
                "auth_error",
                detail=_provider_detail(provider, "auth_error"),
            )
        return DeleteMailResult(
            "connection_error",
            detail=_provider_detail(provider, "connect_error"),
        )

    mutation_in_flight = False
    deleted_count = 0
    emptied_folders = 0

    def failure_result(event: str = "folder_error") -> DeleteMailResult:
        if deleted_count:
            return DeleteMailResult(
                "partial",
                deleted_count=deleted_count,
                folder_count=emptied_folders,
                detail=_provider_detail(provider, "delete_partial"),
            )
        return DeleteMailResult(
            "connection_error",
            detail=_provider_detail(provider, event),
        )

    try:
        capabilities = _imap_capabilities(client)
        if b"UIDPLUS" not in capabilities:
            return DeleteMailResult(
                "connection_error",
                detail=_provider_detail(provider, "delete_error"),
            )
        status, folders = client.list()
        mailboxes, complete = (
            _mailboxes_from_list(folders) if _status_ok(status) else ([], False)
        )
        if not complete or not mailboxes:
            return DeleteMailResult(
                "connection_error",
                detail=_provider_detail(provider, "folder_list_error"),
            )

        filtered = [
            (flags, mailbox)
            for flags, mailbox in mailboxes
            if not (flags & _UNSELECTABLE_MAILBOX_FLAGS)
            and not (flags & _AGGREGATE_MAILBOX_FLAGS)
        ]
        if not filtered:
            return DeleteMailResult(
                "connection_error",
                detail=_provider_detail(provider, "folder_list_error"),
            )

        # A complete Yandex mailbox listing must include INBOX and the standard
        # Trash mailbox.  Do not begin a permanent purge if LIST appears to have
        # omitted part of the store.
        has_inbox = any(
            _mailbox_identity(mailbox) == b"inbox" for _, mailbox in filtered
        )
        has_trash = any(b"\\trash" in flags for flags, _ in filtered)
        if not has_inbox or not has_trash:
            return DeleteMailResult(
                "connection_error",
                detail=_provider_detail(provider, "folder_list_error"),
            )

        # Empty Trash last so messages routed there by server-side behavior during
        # earlier folder processing are included in its later snapshot.
        filtered.sort(key=lambda item: b"\\trash" in item[0])

        for _flags, mailbox in filtered:
            status, _ = client.select(mailbox, readonly=False)
            if not _status_ok(status):
                return failure_result()

            status, search_data = client.uid("SEARCH", None, "ALL")
            if not _status_ok(status):
                return failure_result()
            message_uids = _message_ids_from_search(search_data)
            if message_uids is None:
                return failure_result()
            if not message_uids:
                continue

            try:
                numeric_uids = [int(uid) for uid in message_uids]
                if not numeric_uids or any(uid <= 0 for uid in numeric_uids):
                    raise ValueError
                highest_uid = max(numeric_uids)
            except (TypeError, ValueError):
                return failure_result()

            uid_set = f"1:{highest_uid}"
            mutation_in_flight = True
            status, _ = client.uid(
                "STORE",
                uid_set,
                "+FLAGS.SILENT",
                r"(\Deleted)",
            )
            if not _status_ok(status):
                return DeleteMailResult(
                    "outcome_unknown",
                    deleted_count=deleted_count,
                    folder_count=emptied_folders,
                    detail=_provider_detail(provider, "delete_unknown"),
                )
            status, _ = client.uid("EXPUNGE", uid_set)
            if not _status_ok(status):
                return DeleteMailResult(
                    "outcome_unknown",
                    deleted_count=deleted_count,
                    folder_count=emptied_folders,
                    detail=_provider_detail(provider, "delete_unknown"),
                )
            status, remaining_data = client.uid("SEARCH", None, "UID", uid_set)
            remaining_uids = _message_ids_from_search(remaining_data)
            if (
                not _status_ok(status)
                or remaining_uids is None
                or remaining_uids
            ):
                return DeleteMailResult(
                    "outcome_unknown",
                    deleted_count=deleted_count,
                    folder_count=emptied_folders,
                    detail=_provider_detail(provider, "delete_unknown"),
                )
            mutation_in_flight = False
            deleted_count += len(message_uids)
            emptied_folders += 1

        if not deleted_count:
            return DeleteMailResult(
                "empty",
                detail="Every selectable Yandex mail folder is already empty.",
            )
        return DeleteMailResult(
            "deleted",
            deleted_count=deleted_count,
            folder_count=emptied_folders,
            detail=(
                f"Permanently deleted {deleted_count} message(s) from "
                f"{emptied_folders} Yandex folder(s)."
            ),
        )
    except Exception:
        if mutation_in_flight:
            return DeleteMailResult(
                "outcome_unknown",
                deleted_count=deleted_count,
                folder_count=emptied_folders,
                detail=_provider_detail(provider, "delete_unknown"),
            )
        return failure_result("connection_failed")
    finally:
        _safe_logout(client)


def fetch_latest_code(
    account: Account,
    max_messages: int = 50,
    max_age_hours: int = 72,
    timeout: int = 20,
    access_token_provider: Optional[Callable[[Account], Optional[str]]] = None,
) -> FetchResult:
    """Fetch the newest verification code without marking messages as read.

    Microsoft accounts require ``access_token_provider``.  The callback is
    called with ``account`` and must return a current OAuth access token.  A
    missing, blank, invalid, or failing callback is reported as
    ``oauth_required`` without opening an IMAP connection.
    """

    # Zero explicitly means every message returned by the bounded IMAP search.
    max_messages = max(0, int(max_messages))
    max_age_hours = max(0, int(max_age_hours))
    timeout = max(1, int(timeout))

    provider, imap_host, provider_error = _resolve_provider(account)
    if provider is None:
        return FetchResult("unsupported_provider", detail=provider_error)

    access_token = ""
    if provider.auth_method == "oauth2":
        if access_token_provider is not None:
            try:
                candidate = access_token_provider(account)
                if isinstance(candidate, str):
                    access_token = candidate.strip()
            except Exception:
                access_token = ""
        if not access_token:
            return FetchResult(
                "oauth_required",
                detail=_provider_detail(provider, "oauth_required"),
            )

    client, error_status = _connect_and_login(
        account,
        timeout,
        provider=provider,
        imap_host=imap_host,
        access_token=access_token,
    )
    if client is None:
        if error_status == "auth_error":
            return FetchResult("auth_error", detail=_provider_detail(provider, "auth_error"))
        if error_status == "oauth_required":
            return FetchResult(
                "oauth_required",
                detail=_provider_detail(provider, "oauth_required"),
            )
        return FetchResult("connection_error", detail=_provider_detail(provider, "connect_error"))

    try:
        status, _ = client.select("INBOX", readonly=True)
        if not _status_ok(status):
            return FetchResult(
                "connection_error",
                detail=_provider_detail(provider, "inbox_error"),
            )

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        if max_age_hours:
            # IMAP SINCE uses INTERNALDATE at day precision.  The Date-header
            # check below applies the exact hour boundary after downloading.
            status, search_data = client.search(
                None,
                "SINCE",
                cutoff.strftime("%d-%b-%Y"),
            )
        else:
            status, search_data = client.search(None, "ALL")
        if not _status_ok(status) or not search_data:
            return FetchResult(
                "connection_error",
                detail=_provider_detail(provider, "list_error"),
            )
        first = search_data[0]
        if isinstance(first, str):
            message_ids = first.encode("ascii", errors="ignore").split()
        elif isinstance(first, bytes):
            message_ids = first.split()
        else:
            message_ids = []

        inspected = 0
        recent_messages: list[MailMessage] = []
        newest_any_code_message: Optional[MailMessage] = None
        newest_alias_code_message: Optional[MailMessage] = None
        candidate_ids = message_ids[-max_messages:] if max_messages else message_ids

        # Fetch small INTERNALDATE records first so an exact one-hour window
        # does not require downloading every message received since midnight.
        if max_age_hours and candidate_ids:
            server_dates: dict[bytes, datetime] = {}
            for start in range(0, len(candidate_ids), 250):
                id_chunk = candidate_ids[start:start + 250]
                status, date_data = client.fetch(b",".join(id_chunk), "(INTERNALDATE)")
                if _status_ok(status):
                    server_dates.update(_internal_dates_from_fetch(date_data))
            candidate_ids = [
                message_id
                for message_id in candidate_ids
                if message_id not in server_dates or server_dates[message_id] >= cutoff
            ]

        # Message bodies are fetched in sequence-set batches.  This is
        # materially faster for busy Facebook registration inboxes while still
        # using BODY.PEEK so messages remain unread.
        records: list[tuple[bytes, bytes, Optional[datetime]]] = []
        end = len(candidate_ids)
        while end > 0:
            start = max(0, end - 50)
            id_chunk = candidate_ids[start:end]
            status, fetch_data = client.fetch(
                b",".join(id_chunk),
                "(BODY.PEEK[] INTERNALDATE)",
            )
            chunk_records = (
                _message_records_from_fetch(fetch_data) if _status_ok(status) else []
            )
            if not chunk_records:
                # A server may reject a large sequence set even though an
                # individual message remains readable.
                for message_id in reversed(id_chunk):
                    item_status, item_data = client.fetch(
                        message_id,
                        "(BODY.PEEK[] INTERNALDATE)",
                    )
                    if _status_ok(item_status):
                        records.extend(_message_records_from_fetch(item_data))
            else:
                by_id = {
                    message_id: record
                    for record in chunk_records
                    if (message_id := record[0])
                }
                unkeyed = [record for record in chunk_records if not record[0]]
                records.extend(
                    by_id[message_id]
                    for message_id in reversed(id_chunk)
                    if message_id in by_id
                )
                records.extend(reversed(unkeyed))
            end = start

        for _message_id, raw_message, server_received_at in records:
            inspected += 1
            try:
                subject, sender, received, body, received_at = _parse_message(raw_message)
            except (TypeError, ValueError, UnicodeError):
                continue
            effective_received_at = server_received_at or received_at
            if (
                max_age_hours
                and effective_received_at is not None
                and effective_received_at < cutoff
            ):
                continue
            code = extract_verification_code(subject, body)
            recipient_addresses = _message_recipient_addresses(raw_message)
            recipient = _preferred_recipient_address(
                recipient_addresses,
                account.email,
            )
            facebook_name = extract_facebook_name(subject, sender, body)
            message = MailMessage(
                code=code or "",
                subject=subject,
                sender=sender,
                received=received,
                body=body[:_MAX_RESULT_BODY_CHARS],
                recipient=recipient,
                facebook_name=facebook_name,
                received_timestamp=(
                    effective_received_at.timestamp()
                    if effective_received_at is not None
                    else 0.0
                ),
            )
            recent_messages.append(message)
            if code and newest_any_code_message is None:
                newest_any_code_message = message
            if (
                code
                and account.mail_alias
                and newest_alias_code_message is None
                and any(
                    address.casefold() == account.mail_alias.casefold()
                    for address in recipient_addresses
                )
            ):
                newest_alias_code_message = message

        messages = tuple(recent_messages)
        newest_code_message = newest_alias_code_message or newest_any_code_message
        if newest_code_message is not None:
            detail = ""
            if account.mail_alias and newest_alias_code_message is None:
                detail = (
                    "The configured alias did not match a recent code message; "
                    "the newest inbox code is shown instead."
                )
            return FetchResult(
                "found",
                code=newest_code_message.code,
                subject=newest_code_message.subject,
                sender=newest_code_message.sender,
                received=newest_code_message.received,
                detail=detail,
                body=newest_code_message.body,
                messages=messages,
                recipient=newest_code_message.recipient,
                facebook_name=newest_code_message.facebook_name,
            )

        return FetchResult(
            "not_found",
            detail=(
                f"No verification code found in {len(recent_messages)} message(s) "
                f"inside the selected time window ({inspected} inspected)."
            ),
            messages=messages,
        )
    except (OSError, imaplib.IMAP4.error, TimeoutError):
        return FetchResult(
            "connection_error",
            detail=_provider_detail(provider, "connection_failed"),
        )
    finally:
        _safe_logout(client)


__all__ = [
    "Account",
    "DeleteMailResult",
    "FetchResult",
    "MailMessage",
    "PROVIDER_SPECS",
    "ProviderSpec",
    "ZOHO_IMAP_PRESETS",
    "decode_message_body",
    "decode_mime_header",
    "detect_provider",
    "extract_facebook_name",
    "extract_verification_code",
    "fetch_latest_code",
    "delete_all_yandex_mail",
    "parse_account_lines",
    "provider_label",
    "strip_html",
]
