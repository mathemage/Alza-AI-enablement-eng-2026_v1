import base64
import binascii
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.errors import HeaderParseError
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser
from typing import cast

from alza_ai.domain import Attachment, InboundEmail

_MALFORMED_MESSAGE = "mime_malformed_message"
_MAX_ATTACHMENTS = 5
_MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
_MAX_TOTAL_SIZE = 24 * 1024 * 1024
_MAX_MIME_DEPTH = 50
_BASE64URL = re.compile(r"[A-Za-z0-9_-]*={0,2}")
_BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_REFERENCE = re.compile(r"<[^<>\s]+>")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_HTML_HIDDEN = frozenset({"head", "script", "style"})
_HTML_SEPARATORS = frozenset(
    {
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "table",
        "td",
        "th",
        "tr",
    }
)


class MimeParseError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _BodySource:
    data: str | None
    attachment_id: str | None


@dataclass(frozen=True, slots=True)
class _Part:
    order: int
    part_id: str
    mime_type: str
    filename: str
    filename_present: bool
    disposition: str | None
    content_id: str | None
    charset: str
    body: _BodySource
    opaque: bool
    children: tuple[_Part, ...]


@dataclass(frozen=True, slots=True)
class _TextFragment:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class _MediaSpec:
    family: str
    canonical_type: str
    signature: str


_MEDIA_TYPES = {
    "application/pdf": _MediaSpec("document", "application/pdf", "pdf"),
    "audio/mpeg": _MediaSpec("audio", "audio/mpeg", "mp3"),
    "audio/wav": _MediaSpec("audio", "audio/wav", "wav"),
    "audio/x-wav": _MediaSpec("audio", "audio/wav", "wav"),
    "image/jpeg": _MediaSpec("image", "image/jpeg", "jpeg"),
    "image/png": _MediaSpec("image", "image/png", "png"),
}


class _PartBuilder:
    def __init__(self) -> None:
        self._next_order = 0

    def build(self, value: object, depth: int = 0) -> _Part:
        if depth > _MAX_MIME_DEPTH:
            raise MimeParseError(_MALFORMED_MESSAGE)
        part = _mapping(value)
        mime_type = _required_string(part, "mimeType").strip().casefold()
        if not mime_type:
            raise MimeParseError(_MALFORMED_MESSAGE)
        part_id = _optional_string(part, "partId") or ""
        headers = _headers(part)

        raw_filename = _optional_string(part, "filename") or ""
        decoded_filename = _decode_header_value(raw_filename)
        filename_present = bool(decoded_filename.strip())
        filename = _sanitize_filename(decoded_filename)
        disposition = _disposition(headers)
        content_id = _content_id(headers)
        charset = _charset(headers)
        body = _body_source(part)
        children: tuple[_Part, ...]
        opaque = False

        if mime_type.startswith("multipart/"):
            if filename_present or disposition == "attachment":
                raise MimeParseError("mime_unsupported_attachment_type")
            raw_children = part.get("parts")
            if not isinstance(raw_children, list):
                raise MimeParseError(_MALFORMED_MESSAGE)
            children = tuple(
                self.build(child, depth + 1)
                for child in cast(list[object], raw_children)
            )
            opaque = disposition == "inline" or content_id is not None
        else:
            if "parts" in part:
                raise MimeParseError(_MALFORMED_MESSAGE)
            children = ()

        order = self._next_order
        self._next_order += 1
        return _Part(
            order=order,
            part_id=part_id,
            mime_type=mime_type,
            filename=filename,
            filename_present=filename_present,
            disposition=disposition,
            content_id=content_id,
            charset=charset,
            body=body,
            opaque=opaque,
            children=children,
        )


class _HtmlContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._hidden: list[str] = []
        self.cid_references: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if self._hidden:
            if normalized_tag in _HTML_HIDDEN:
                self._hidden.append(normalized_tag)
            return
        if normalized_tag in _HTML_HIDDEN:
            self._hidden.append(normalized_tag)
            return
        if normalized_tag in _HTML_SEPARATORS:
            self._chunks.append(" ")
        for _, value in attrs:
            if value is None:
                continue
            candidate = value.strip()
            if candidate[:4].casefold() != "cid:":
                continue
            content_id = _normalize_content_id(candidate[4:])
            if content_id:
                self.cid_references.add(content_id.casefold())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._hidden:
            if normalized_tag == self._hidden[-1]:
                self._hidden.pop()
            return
        if normalized_tag in _HTML_SEPARATORS:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


def parse_inbound_email(
    mailbox_key: str,
    message: Mapping[str, object],
    external_attachments: Mapping[str, bytes] | None = None,
) -> InboundEmail:
    if not isinstance(mailbox_key, str) or not mailbox_key:
        raise MimeParseError(_MALFORMED_MESSAGE)

    external: Mapping[str, bytes] = (
        {} if external_attachments is None else external_attachments
    )
    if not isinstance(external, Mapping):
        raise MimeParseError(_MALFORMED_MESSAGE)
    for attachment_id, external_data in external.items():
        if not isinstance(attachment_id, str) or not isinstance(external_data, bytes):
            raise MimeParseError(_MALFORMED_MESSAGE)

    source = _mapping(message)
    message_id = _required_string(source, "id")
    thread_id = _required_string(source, "threadId")
    received_at = _received_at(source.get("internalDate"))
    payload = _mapping(source.get("payload"))
    root_headers = _headers(payload)

    rfc_message_id = _required_header(root_headers, "message-id")
    sender = _required_header(root_headers, "from")
    subject = _singleton_header(root_headers, "subject") or ""
    reply_to = _singleton_header(root_headers, "reply-to")
    references = _references(root_headers)
    auto_submitted = _singleton_header(root_headers, "auto-submitted")
    precedence = _singleton_header(root_headers, "precedence")
    list_id = _singleton_header(root_headers, "list-id")
    auto_response_suppress = _singleton_header(root_headers, "x-auto-response-suppress")

    root = _PartBuilder().build(payload)
    leaves = _leaves(root)
    text_fragments: dict[int, _TextFragment] = {}
    cid_references: set[str] = set()

    for part in leaves:
        if part.mime_type not in {"text/plain", "text/html"}:
            continue
        if part.filename_present or part.disposition == "attachment":
            continue
        decoded_text = _decode_text(part, external)
        if part.mime_type == "text/plain":
            text_fragments[part.order] = _TextFragment(
                "plain", _normalize_plain(decoded_text)
            )
            continue
        html_parser = _HtmlContentParser()
        try:
            html_parser.feed(decoded_text)
            html_parser.close()
        except ValueError:
            raise MimeParseError(_MALFORMED_MESSAGE) from None
        text_fragments[part.order] = _TextFragment("html", html_parser.text())
        cid_references.update(html_parser.cid_references)

    candidates: list[_Part] = []
    candidate_orders: set[int] = set()
    warnings: list[str] = []
    for part in leaves:
        referenced = (
            part.content_id is not None and part.content_id.casefold() in cid_references
        )
        is_attachment = (
            part.filename_present or part.disposition == "attachment" or referenced
        )
        if is_attachment:
            if not part.part_id:
                raise MimeParseError(_MALFORMED_MESSAGE)
            candidates.append(part)
            candidate_orders.add(part.order)
            if len(candidates) > _MAX_ATTACHMENTS:
                raise MimeParseError("mime_too_many_attachments")
        elif part.mime_type not in {"text/plain", "text/html"} and (
            part.disposition == "inline" or part.content_id is not None
        ):
            _add_warning(warnings, "mime_ignored_decorative_inline")

    for part in _all_parts(root):
        if part.order not in candidate_orders and part.order not in text_fragments:
            _validate_part_source(part, external)

    attachments: list[Attachment] = []
    total_size = 0
    for part in candidates:
        media = _MEDIA_TYPES.get(part.mime_type)
        if media is None:
            raise MimeParseError("mime_unsupported_attachment_type")
        resolved_data: bytes | None
        if part.body.data is None:
            external_or_empty_data = _part_bytes(part, external)
            resolved_data = external_or_empty_data
            size = len(external_or_empty_data)
        else:
            resolved_data = None
            size = _base64url_decoded_size(part.body.data)
        if size > _MAX_ATTACHMENT_SIZE:
            raise MimeParseError("mime_attachment_too_large")
        total_size += size
        if total_size > _MAX_TOTAL_SIZE:
            raise MimeParseError("mime_attachments_too_large")
        attachment_data = (
            _decode_base64url(part.body.data)
            if resolved_data is None and part.body.data is not None
            else resolved_data
        )
        if attachment_data is None:
            raise MimeParseError(_MALFORMED_MESSAGE)
        if not _matches_signature(media.signature, attachment_data):
            raise MimeParseError("mime_attachment_type_mismatch")
        referenced = (
            part.content_id is not None and part.content_id.casefold() in cid_references
        )
        disposition = (
            "inline"
            if part.disposition == "inline" or (part.disposition is None and referenced)
            else "attachment"
        )
        attachments.append(
            Attachment(
                part_id=part.part_id,
                filename=part.filename,
                media_family=media.family,
                media_type=media.canonical_type,
                disposition=disposition,
                content_id=part.content_id,
                size=size,
                data=attachment_data,
            )
        )

    body_fragments = _select_body(root, candidate_orders, text_fragments)
    text = "\n".join(fragment.text for fragment in body_fragments if fragment.text)
    text = text.strip()
    if not text:
        raise MimeParseError("mime_missing_body")

    return InboundEmail(
        mailbox_key=mailbox_key,
        message_id=message_id,
        thread_id=thread_id,
        rfc_message_id=rfc_message_id,
        subject=subject,
        sender=sender,
        reply_to=reply_to,
        references=references,
        received_at=received_at,
        text=text,
        attachments=tuple(attachments),
        warnings=tuple(warnings),
        auto_submitted=auto_submitted,
        precedence=precedence,
        list_id=list_id,
        auto_response_suppress=auto_response_suppress,
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MimeParseError(_MALFORMED_MESSAGE)
    return cast(Mapping[str, object], value)


def _required_string(source: Mapping[str, object], name: str) -> str:
    value = source.get(name)
    if not isinstance(value, str) or not value:
        raise MimeParseError(_MALFORMED_MESSAGE)
    return value


def _optional_string(source: Mapping[str, object], name: str) -> str | None:
    if name not in source:
        return None
    value = source[name]
    if not isinstance(value, str):
        raise MimeParseError(_MALFORMED_MESSAGE)
    return value


def _headers(part: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    raw_headers = part.get("headers", [])
    if not isinstance(raw_headers, list):
        raise MimeParseError(_MALFORMED_MESSAGE)
    grouped: dict[str, list[str]] = {}
    for raw_header in cast(list[object], raw_headers):
        header = _mapping(raw_header)
        name = header.get("name")
        value = header.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise MimeParseError(_MALFORMED_MESSAGE)
        grouped.setdefault(name.casefold(), []).append(value)
    return {name: tuple(values) for name, values in grouped.items()}


def _raw_singleton(headers: Mapping[str, tuple[str, ...]], name: str) -> str | None:
    values = headers.get(name, ())
    if len(values) > 1:
        raise MimeParseError(_MALFORMED_MESSAGE)
    return values[0] if values else None


def _singleton_header(
    headers: Mapping[str, tuple[str, ...]],
    name: str,
    *,
    required: bool = False,
) -> str | None:
    raw = _raw_singleton(headers, name)
    if raw is None:
        if required:
            raise MimeParseError(_MALFORMED_MESSAGE)
        return None
    value = _decode_header_value(raw).strip()
    if required and not value:
        raise MimeParseError(_MALFORMED_MESSAGE)
    return value or None


def _required_header(headers: Mapping[str, tuple[str, ...]], name: str) -> str:
    value = _singleton_header(headers, name, required=True)
    if value is None:
        raise MimeParseError(_MALFORMED_MESSAGE)
    return value


def _decode_header_value(raw: str) -> str:
    try:
        decoded: list[str] = []
        for value, charset in decode_header(raw):
            if isinstance(value, bytes):
                decoded.append(value.decode(charset or "ascii", errors="strict"))
            else:
                decoded.append(value)
        unfolded = re.sub(r"\r?\n[ \t]+", " ", "".join(decoded))
    except HeaderParseError, LookupError, UnicodeError, ValueError:
        raise MimeParseError(_MALFORMED_MESSAGE) from None
    if "\r" in unfolded or "\n" in unfolded:
        raise MimeParseError(_MALFORMED_MESSAGE)
    return unfolded


def _references(headers: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    references: list[str] = []
    for raw in headers.get("references", ()):
        references.extend(_REFERENCE.findall(_decode_header_value(raw)))
    return tuple(references)


def _disposition(headers: Mapping[str, tuple[str, ...]]) -> str | None:
    raw = _raw_singleton(headers, "content-disposition")
    if raw is None:
        return None
    token = _decode_header_value(raw).split(";", 1)[0].strip().casefold()
    return token if token in {"attachment", "inline"} else None


def _content_id(headers: Mapping[str, tuple[str, ...]]) -> str | None:
    raw = _raw_singleton(headers, "content-id")
    if raw is None:
        return None
    return _normalize_content_id(_decode_header_value(raw)) or None


def _normalize_content_id(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1].strip()
    return normalized


def _charset(headers: Mapping[str, tuple[str, ...]]) -> str:
    raw = _raw_singleton(headers, "content-type")
    if raw is None:
        return "utf-8"
    message = Message()
    try:
        message["Content-Type"] = _decode_header_value(raw)
        return message.get_content_charset() or "utf-8"
    except LookupError, ValueError:
        raise MimeParseError(_MALFORMED_MESSAGE) from None


def _body_source(part: Mapping[str, object]) -> _BodySource:
    body = _mapping(part.get("body"))
    has_data = "data" in body
    has_attachment = "attachmentId" in body
    if has_data and has_attachment:
        raise MimeParseError(_MALFORMED_MESSAGE)
    data = _optional_string(body, "data") if has_data else None
    attachment_id = _optional_string(body, "attachmentId") if has_attachment else None
    if has_attachment and not attachment_id:
        raise MimeParseError(_MALFORMED_MESSAGE)
    return _BodySource(data=data, attachment_id=attachment_id)


def _received_at(value: object) -> datetime:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise MimeParseError(_MALFORMED_MESSAGE)
    try:
        return _EPOCH + timedelta(milliseconds=int(value))
    except OverflowError, ValueError:
        raise MimeParseError(_MALFORMED_MESSAGE) from None


def _sanitize_filename(value: str) -> str:
    filename = value.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(
        character
        for character in filename
        if not unicodedata.category(character).startswith("C")
    )
    filename = filename.strip().strip(".").strip()
    return filename[:255] or "attachment"


def _leaves(part: _Part) -> tuple[_Part, ...]:
    if part.opaque or not part.children:
        return (part,)
    return tuple(leaf for child in part.children for leaf in _leaves(child))


def _all_parts(part: _Part) -> tuple[_Part, ...]:
    return (part, *(node for child in part.children for node in _all_parts(child)))


def _validate_part_source(part: _Part, external: Mapping[str, bytes]) -> None:
    if part.mime_type in {"text/plain", "text/html"} and (
        part.body.data is not None or part.body.attachment_id is not None
    ):
        _decode_text(part, external)
    elif part.body.data is not None:
        _base64url_decoded_size(part.body.data)
    elif (
        part.body.attachment_id is not None and part.body.attachment_id not in external
    ):
        raise MimeParseError("mime_missing_attachment_data")


def _part_bytes(part: _Part, external: Mapping[str, bytes]) -> bytes:
    if part.body.data is not None:
        return _decode_base64url(part.body.data)
    if part.body.attachment_id is None:
        return b""
    try:
        return external[part.body.attachment_id]
    except KeyError:
        raise MimeParseError("mime_missing_attachment_data") from None


def _decode_base64url(value: str) -> bytes:
    _base64url_decoded_size(value)
    core = value.rstrip("=")
    required_padding = (-len(core)) % 4
    try:
        return base64.b64decode(
            core + "=" * required_padding,
            altchars=b"-_",
            validate=True,
        )
    except binascii.Error, ValueError:
        raise MimeParseError("mime_malformed_base64url") from None


def _base64url_decoded_size(value: str) -> int:
    if _BASE64URL.fullmatch(value) is None:
        raise MimeParseError("mime_malformed_base64url")
    core = value.rstrip("=")
    supplied_padding = len(value) - len(core)
    if len(core) % 4 == 1:
        raise MimeParseError("mime_malformed_base64url")
    required_padding = (-len(core)) % 4
    if supplied_padding not in {0, required_padding}:
        raise MimeParseError("mime_malformed_base64url")
    remainder = len(core) % 4
    if remainder in {2, 3}:
        trailing_value = _BASE64URL_ALPHABET.index(core[-1])
        unused_mask = 0x0F if remainder == 2 else 0x03
        if trailing_value & unused_mask:
            raise MimeParseError("mime_malformed_base64url")
    return len(core) // 4 * 3 + {0: 0, 2: 1, 3: 2}[remainder]


def _decode_text(part: _Part, external: Mapping[str, bytes]) -> str:
    data = _part_bytes(part, external)
    try:
        return data.decode(part.charset, errors="strict")
    except LookupError, UnicodeError:
        raise MimeParseError(_MALFORMED_MESSAGE) from None


def _normalize_plain(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" \t") for line in normalized.split("\n")).strip()


def _select_body(
    part: _Part,
    attachment_orders: set[int],
    fragments: Mapping[int, _TextFragment],
) -> tuple[_TextFragment, ...]:
    if part.opaque or not part.children:
        if part.order in attachment_orders:
            return ()
        fragment = fragments.get(part.order)
        return () if fragment is None or not fragment.text else (fragment,)

    selected = tuple(
        fragment
        for child in part.children
        for fragment in _select_body(child, attachment_orders, fragments)
    )
    if part.mime_type != "multipart/alternative":
        return selected
    for kind in ("plain", "html"):
        for fragment in selected:
            if fragment.kind == kind and fragment.text:
                return (fragment,)
    return ()


def _add_warning(warnings: list[str], code: str) -> None:
    if code not in warnings and len(warnings) < 10:
        warnings.append(code)


def _matches_signature(signature: str, data: bytes) -> bool:
    if signature == "pdf":
        return data.startswith(b"%PDF-")
    if signature == "mp3":
        return data.startswith(b"ID3") or (
            len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
        )
    if signature == "wav":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if signature == "jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if signature == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    raise AssertionError(signature)
