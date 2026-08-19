import base64
import copy
import logging
import socket
import urllib.request
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Never, cast

import pytest

from alza_ai.domain import Attachment, InboundEmail
from alza_ai.mime import MimeParseError, parse_inbound_email

JsonObject = dict[str, object]

MAILBOX_KEY = "mailbox-key"
MESSAGE_ID = "message-1"
THREAD_ID = "thread-1"
RFC_MESSAGE_ID = "<source@example.test>"
RECEIVED_AT = datetime(2024, 1, 1, tzinfo=UTC)
MIB = 1024 * 1024
PER_ATTACHMENT_LIMIT = 20 * MIB
TOTAL_ATTACHMENT_LIMIT = 24 * MIB


def header(name: str, value: object) -> JsonObject:
    return {"name": name, "value": value}


def message_headers() -> list[JsonObject]:
    return [
        header("mEsSaGe-Id", RFC_MESSAGE_ID),
        header(
            "sUbJeCt",
            "=?UTF-8?Q?P=C5=99ehled_objedn=C3=A1vky?=",
        ),
        header(
            "fRoM",
            "=?UTF-8?Q?Jana_Nov=C3=A1?= <jana@example.test>",
        ),
        header(
            "rEpLy-To",
            "=?UTF-8?Q?Podpora?= <reply@example.test>",
        ),
        header("References", "<root@example.test>\r\n\t<parent@example.test>"),
        header("rEfErEnCeS", "<previous@example.test>"),
    ]


def encode_base64url(value: bytes, *, padded: bool = False) -> str:
    encoded = base64.urlsafe_b64encode(value).decode("ascii")
    return encoded if padded else encoded.rstrip("=")


def text_part(
    part_id: str,
    text: str,
    *,
    mime_type: str = "text/plain",
    padded: bool = False,
    charset: str = "utf-8",
) -> JsonObject:
    content = text.encode("utf-8")
    return {
        "partId": part_id,
        "mimeType": mime_type,
        "filename": "",
        "headers": [
            header("Content-Type", f"{mime_type}; charset={charset}"),
        ],
        "body": {
            "size": len(content),
            "data": encode_base64url(content, padded=padded),
        },
    }


def multipart(part_id: str, subtype: str, parts: list[JsonObject]) -> JsonObject:
    mime_type = f"multipart/{subtype}"
    return {
        "partId": part_id,
        "mimeType": mime_type,
        "filename": "",
        "headers": [header("Content-Type", mime_type)],
        "body": {"size": 0},
        "parts": parts,
    }


def attachment_part(
    part_id: str,
    mime_type: str,
    *,
    filename: str = "",
    data: bytes | None = None,
    attachment_id: str | None = None,
    disposition: str | None = "attachment",
    content_id: str | None = None,
    reported_size: int | None = None,
    padded: bool = False,
) -> JsonObject:
    body: JsonObject = {
        "size": reported_size if reported_size is not None else len(data or b""),
    }
    if data is not None:
        body["data"] = encode_base64url(data, padded=padded)
    if attachment_id is not None:
        body["attachmentId"] = attachment_id

    headers = [header("Content-Type", mime_type)]
    if disposition is not None:
        headers.append(header("Content-Disposition", disposition))
    if content_id is not None:
        headers.append(header("Content-ID", content_id))
    return {
        "partId": part_id,
        "mimeType": mime_type,
        "filename": filename,
        "headers": headers,
        "body": body,
    }


def gmail_message(payload: JsonObject) -> JsonObject:
    root = copy.deepcopy(payload)
    part_headers = root.get("headers", [])
    assert isinstance(part_headers, list)
    root["headers"] = [*message_headers(), *part_headers]
    return {
        "id": MESSAGE_ID,
        "threadId": THREAD_ID,
        "internalDate": "1704067200000",
        "payload": root,
    }


def payload_of(message: JsonObject) -> JsonObject:
    payload = message["payload"]
    assert isinstance(payload, dict)
    return cast(JsonObject, payload)


def headers_of(message: JsonObject) -> list[JsonObject]:
    headers = payload_of(message)["headers"]
    assert isinstance(headers, list)
    return cast(list[JsonObject], headers)


def synthetic_file(kind: str, size: int | None = None) -> bytes:
    if kind == "pdf":
        prefix = b"%PDF-1.7\n"
    elif kind == "mp3-id3":
        prefix = b"ID3\x04\x00\x00\x00\x00\x00\x00"
    elif kind == "mp3-frame":
        prefix = b"\xff\xfb\x90\x64"
    elif kind == "wav":
        target_size = 28 if size is None else size
        prefix = b"RIFF" + (target_size - 8).to_bytes(4, "little") + b"WAVE"
    elif kind == "jpeg":
        prefix = b"\xff\xd8\xff\xe0"
    elif kind == "png":
        prefix = b"\x89PNG\r\n\x1a\n"
    else:
        raise ValueError(kind)

    if kind != "wav":
        target_size = len(prefix) + 16 if size is None else size
    if target_size < len(prefix):
        raise ValueError("synthetic file is smaller than its signature")
    return prefix + b"x" * (target_size - len(prefix))


def external_attachment_message(
    contents: list[bytes],
) -> tuple[JsonObject, dict[str, bytes]]:
    parts = [text_part("0", "Boundary body")]
    external: dict[str, bytes] = {}
    for index, content in enumerate(contents, start=1):
        attachment_id = f"attachment-{index}"
        external[attachment_id] = content
        parts.append(
            attachment_part(
                str(index),
                "application/pdf",
                filename=f"file-{index}.pdf",
                attachment_id=attachment_id,
                reported_size=1,
            )
        )
    return gmail_message(multipart("", "mixed", parts)), external


def assert_parse_error(
    message: JsonObject,
    code: str,
    external_attachments: dict[str, bytes] | None = None,
) -> MimeParseError:
    with pytest.raises(MimeParseError) as raised:
        parse_inbound_email(
            MAILBOX_KEY,
            message,
            external_attachments=external_attachments,
        )

    assert raised.value.code == code
    assert str(raised.value) == code
    return raised.value


def test_mime_01_plain_text_maps_headers_identifiers_and_normalized_body() -> None:
    message = gmail_message(text_part("", "  První řádek  \r\nDruhý řádek\t \r\n\r\n"))

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed == InboundEmail(
        mailbox_key=MAILBOX_KEY,
        message_id=MESSAGE_ID,
        thread_id=THREAD_ID,
        rfc_message_id=RFC_MESSAGE_ID,
        subject="Přehled objednávky",
        sender="Jana Nová <jana@example.test>",
        reply_to="Podpora <reply@example.test>",
        references=(
            "<root@example.test>",
            "<parent@example.test>",
            "<previous@example.test>",
        ),
        received_at=RECEIVED_AT,
        text="První řádek\nDruhý řádek",
        attachments=(),
        warnings=(),
    )


def test_mime_01_optional_subject_and_reply_to_have_stable_defaults() -> None:
    message = gmail_message(text_part("", "Body"))
    headers = headers_of(message)
    headers[:] = [
        item
        for item in headers
        if str(item.get("name", "")).casefold() not in {"subject", "reply-to"}
    ]

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.subject == ""
    assert parsed.reply_to is None


def test_mime_01_html_is_converted_locally_without_remote_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_io(*args: object, **kwargs: object) -> Never:
        raise AssertionError("MIME parsing attempted network I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden_io)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_io)
    html = """
        <html><head>
          <style>.private { display: none }</style>
          <script>fetch('https://remote.example/script')</script>
        </head><body>
          <p>Hello&nbsp;<strong>world</strong>.</p>
          <!-- private comment -->
          <img src="https://remote.example/private.png" alt="remote image">
          <div>Second &amp; final.</div>
        </body></html>
    """

    parsed = parse_inbound_email(
        MAILBOX_KEY,
        gmail_message(text_part("", html, mime_type="text/html")),
    )

    assert parsed.text == "Hello world. Second & final."
    assert "remote.example" not in parsed.text
    assert "private" not in parsed.text


def test_mime_01_multipart_alternative_prefers_plain_regardless_of_order() -> None:
    message = gmail_message(
        multipart(
            "",
            "alternative",
            [
                text_part("0", "<p>Duplicate HTML</p>", mime_type="text/html"),
                text_part("1", "Preferred plain"),
            ],
        )
    )

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.text == "Preferred plain"


def test_mime_01_empty_plain_alternative_falls_back_to_html() -> None:
    message = gmail_message(
        multipart(
            "",
            "alternative",
            [
                text_part("0", " \r\n"),
                text_part("1", "<p>HTML fallback</p>", mime_type="text/html"),
            ],
        )
    )

    assert parse_inbound_email(MAILBOX_KEY, message).text == "HTML fallback"


def test_mime_01_adversarial_nested_multiparts_preserve_fragment_order() -> None:
    message = gmail_message(
        multipart(
            "",
            "mixed",
            [
                text_part("0", "First"),
                multipart(
                    "1",
                    "mixed",
                    [
                        multipart(
                            "1.0",
                            "alternative",
                            [
                                text_part(
                                    "1.0.0",
                                    "<p>Duplicate</p>",
                                    mime_type="text/html",
                                ),
                                text_part("1.0.1", "Chosen"),
                            ],
                        ),
                        multipart(
                            "1.1",
                            "related",
                            [
                                text_part(
                                    "1.1.0",
                                    "<div>Third</div>",
                                    mime_type="text/html",
                                )
                            ],
                        ),
                    ],
                ),
            ],
        )
    )

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.text == "First\nChosen\nThird"


def test_mime_01_cid_in_unselected_html_makes_inline_media_an_attachment() -> None:
    png = synthetic_file("png")
    message = gmail_message(
        multipart(
            "",
            "related",
            [
                multipart(
                    "0",
                    "alternative",
                    [
                        text_part("0.0", "Plain body"),
                        text_part(
                            "0.1",
                            '<p>HTML body</p><img src="CID:ChArT">',
                            mime_type="text/html",
                        ),
                    ],
                ),
                attachment_part(
                    "1",
                    "image/png",
                    data=png,
                    disposition=None,
                    content_id="  <chart>  ",
                ),
            ],
        )
    )

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.text == "Plain body"
    assert parsed.attachments == (
        Attachment(
            part_id="1",
            filename="attachment",
            media_family="image",
            media_type="image/png",
            disposition="inline",
            content_id="chart",
            size=len(png),
            data=png,
        ),
    )


def test_mime_01_decorative_inline_parts_are_ignored_with_one_bounded_warning() -> None:
    png = synthetic_file("png")
    decorative_parts = [
        attachment_part(
            str(index),
            "image/png",
            data=png,
            disposition="inline",
            content_id=f"<unused-{index}>",
        )
        for index in range(1, 13)
    ]
    message = gmail_message(
        multipart("", "mixed", [text_part("0", "Body"), *decorative_parts])
    )

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.attachments == ()
    assert parsed.warnings == ("mime_ignored_decorative_inline",)
    assert len(parsed.warnings) <= 10


def test_mime_01_malformed_decorative_inline_data_remains_terminal() -> None:
    decorative = attachment_part(
        "1",
        "image/png",
        data=synthetic_file("png"),
        disposition="inline",
        content_id="<unused>",
    )
    body = decorative["body"]
    assert isinstance(body, dict)
    body["data"] = "***private***"
    message = gmail_message(
        multipart("", "mixed", [text_part("0", "Body"), decorative])
    )

    assert_parse_error(message, "mime_malformed_base64url")


def test_mime_01_unreferenced_inline_with_a_filename_still_counts() -> None:
    jpeg = synthetic_file("jpeg")
    message = gmail_message(
        multipart(
            "",
            "mixed",
            [
                text_part("0", "Body"),
                attachment_part(
                    "1",
                    "image/jpeg",
                    filename="inline.jpg",
                    data=jpeg,
                    disposition="inline",
                    content_id="<unused>",
                ),
            ],
        )
    )

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].disposition == "inline"
    assert parsed.warnings == ()


@pytest.mark.parametrize("padded", [False, True])
def test_mime_01_canonical_padded_and_unpadded_base64url_are_accepted(
    padded: bool,
) -> None:
    message = gmail_message(text_part("", "a", padded=padded))

    assert parse_inbound_email(MAILBOX_KEY, message).text == "a"


@pytest.mark.parametrize(
    "invalid_data",
    ["***private***", "+w==", "/w==", "A", "YQ=", "YQ===", "Y=Q="],
)
def test_mime_01_malformed_base64url_is_sanitized_and_never_logged(
    invalid_data: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = gmail_message(text_part("", "private body"))
    body = payload_of(message)["body"]
    assert isinstance(body, dict)
    body["data"] = invalid_data
    caplog.set_level(logging.DEBUG)

    error = assert_parse_error(message, "mime_malformed_base64url")

    assert "private" not in str(error)
    assert caplog.records == []


@pytest.mark.parametrize(
    "case",
    [
        "missing-id",
        "missing-thread-id",
        "invalid-thread-id",
        "missing-rfc-message-id",
        "duplicate-rfc-message-id",
        "missing-payload",
        "empty-mailbox-key",
        "invalid-internal-date",
        "missing-from",
        "duplicate-subject",
        "invalid-header-value",
        "multipart-parts-not-list",
        "missing-child-part-id",
        "null-body-data",
        "unknown-charset",
        "invalid-text-bytes",
    ],
)
def test_mime_01_malformed_structure_headers_timestamp_or_charset_is_terminal(
    case: str,
) -> None:
    message = gmail_message(text_part("", "Body"))

    if case == "missing-id":
        del message["id"]
    elif case == "missing-thread-id":
        del message["threadId"]
    elif case == "invalid-thread-id":
        message["threadId"] = 7
    elif case == "missing-rfc-message-id":
        headers_of(message)[:] = [
            item
            for item in headers_of(message)
            if str(item.get("name", "")).casefold() != "message-id"
        ]
    elif case == "duplicate-rfc-message-id":
        headers_of(message).append(header("Message-ID", "<duplicate@example.test>"))
    elif case == "missing-payload":
        del message["payload"]
    elif case == "empty-mailbox-key":
        with pytest.raises(MimeParseError) as raised:
            parse_inbound_email("", message)
        assert raised.value.code == "mime_malformed_message"
        assert str(raised.value) == "mime_malformed_message"
        return
    elif case == "invalid-internal-date":
        message["internalDate"] = "not-decimal"
    elif case == "missing-from":
        headers_of(message)[:] = [
            item
            for item in headers_of(message)
            if str(item.get("name", "")).casefold() != "from"
        ]
    elif case == "duplicate-subject":
        headers_of(message).append(header("Subject", "Second subject"))
    elif case == "invalid-header-value":
        from_header = next(
            item
            for item in headers_of(message)
            if str(item.get("name", "")).casefold() == "from"
        )
        from_header["value"] = 7
    elif case == "multipart-parts-not-list":
        malformed = multipart("", "mixed", [])
        malformed["parts"] = "not-a-list"
        message = gmail_message(malformed)
    elif case == "missing-child-part-id":
        child = attachment_part(
            "1",
            "application/pdf",
            filename="document.pdf",
            data=synthetic_file("pdf"),
        )
        del child["partId"]
        message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), child]))
    elif case == "null-body-data":
        body = payload_of(message)["body"]
        assert isinstance(body, dict)
        body["data"] = None
    elif case == "unknown-charset":
        content_type = headers_of(message)[-1]
        content_type["value"] = "text/plain; charset=unknown-private-charset"
    elif case == "invalid-text-bytes":
        body = payload_of(message)["body"]
        assert isinstance(body, dict)
        body["data"] = encode_base64url(b"\xff")
    else:
        raise AssertionError(case)

    assert_parse_error(message, "mime_malformed_message")


def test_mime_01_body_with_inline_and_external_representations_is_malformed() -> None:
    pdf = synthetic_file("pdf")
    part = attachment_part(
        "1",
        "application/pdf",
        filename="document.pdf",
        data=pdf,
        attachment_id="external-1",
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    assert_parse_error(
        message,
        "mime_malformed_message",
        external_attachments={"external-1": pdf},
    )


def test_mime_01_missing_external_attachment_data_is_terminal() -> None:
    part = attachment_part(
        "1",
        "application/pdf",
        filename="document.pdf",
        attachment_id="missing-private-id",
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    error = assert_parse_error(
        message,
        "mime_missing_attachment_data",
        external_attachments={},
    )

    assert "missing-private-id" not in str(error)


def test_mime_01_non_bytes_external_attachment_data_is_malformed() -> None:
    part = attachment_part(
        "1",
        "application/pdf",
        filename="document.pdf",
        attachment_id="external-1",
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))
    malformed_external = {"external-1": cast(bytes, "private non-bytes value")}

    error = assert_parse_error(
        message,
        "mime_malformed_message",
        external_attachments=malformed_external,
    )

    assert "private non-bytes value" not in str(error)


def test_mime_01_empty_text_is_a_terminal_missing_body() -> None:
    message = gmail_message(text_part("", " \r\n\t \r\n"))

    assert_parse_error(message, "mime_missing_body")


def test_mime_01_excessive_multipart_depth_is_malformed() -> None:
    payload = text_part("leaf", "Body")
    for depth in range(51):
        payload = multipart(str(depth), "mixed", [payload])

    assert_parse_error(gmail_message(payload), "mime_malformed_message")


def test_mime_01_malformed_hidden_html_never_exposes_hidden_text() -> None:
    html = "<head>private</style>still private</head><p>Visible</p>"
    message = gmail_message(text_part("", html, mime_type="text/html"))

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.text == "Visible"
    assert "private" not in parsed.text


def test_mime_01_parsing_is_deterministic_frozen_silent_and_non_mutating(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pdf = synthetic_file("pdf")
    message, external = external_attachment_message([pdf])
    message_before = copy.deepcopy(message)
    external_before = dict(external)
    read_only_external = MappingProxyType(external)
    caplog.set_level(logging.DEBUG)

    first = parse_inbound_email(
        MAILBOX_KEY,
        message,
        external_attachments=read_only_external,
    )
    second = parse_inbound_email(
        MAILBOX_KEY,
        message,
        external_attachments=read_only_external,
    )

    assert first == second
    assert message == message_before
    assert external == external_before
    assert caplog.records == []
    assert "Boundary body" not in repr(first)
    assert "file-1.pdf" not in repr(first)
    assert "%PDF" not in repr(first)
    subject_attribute = "subject"
    with pytest.raises(FrozenInstanceError):
        setattr(first, subject_attribute, "changed")
    filename_attribute = "filename"
    with pytest.raises(FrozenInstanceError):
        setattr(first.attachments[0], filename_attribute, "changed.pdf")


@pytest.mark.parametrize(
    (
        "fixture_name",
        "declared_type",
        "kind",
        "expected_family",
        "expected_type",
    ),
    [
        ("PDF", "application/pdf", "pdf", "document", "application/pdf"),
        ("MP3 ID3", "audio/mpeg", "mp3-id3", "audio", "audio/mpeg"),
        ("MP3 frame", "audio/mpeg", "mp3-frame", "audio", "audio/mpeg"),
        ("WAV", "audio/wav", "wav", "audio", "audio/wav"),
        ("WAV alias", "audio/x-wav", "wav", "audio", "audio/wav"),
        ("Gmail WAV alias", "audio/vnd.wave", "wav", "audio", "audio/wav"),
        ("JPEG", "image/jpeg", "jpeg", "image", "image/jpeg"),
        ("PNG", "IMAGE/PNG", "png", "image", "image/png"),
    ],
)
def test_mime_02_supported_license_safe_fixture_maps_canonical_attachment(
    fixture_name: str,
    declared_type: str,
    kind: str,
    expected_family: str,
    expected_type: str,
) -> None:
    content = synthetic_file(kind)
    filename = f"synthetic-{fixture_name.casefold().replace(' ', '-')}.bin"
    part = attachment_part(
        "1",
        declared_type,
        filename=filename,
        data=content,
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.attachments == (
        Attachment(
            part_id="1",
            filename=filename,
            media_family=expected_family,
            media_type=expected_type,
            disposition="attachment",
            content_id=None,
            size=len(content),
            data=content,
        ),
    )


def test_mime_02_encoded_filename_is_sanitized_and_missing_disposition_defaults() -> (
    None
):
    pdf = synthetic_file("pdf")
    encoded_filename = "..\\private/ \x00=?UTF-8?Q?P=C5=99ehled_objedn=C3=A1vky?=.pdf. "
    part = attachment_part(
        "1",
        "application/pdf",
        filename=encoded_filename,
        data=pdf,
        disposition=None,
        content_id="<unused-document>",
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    attachment = parse_inbound_email(MAILBOX_KEY, message).attachments[0]

    assert attachment.filename == "Přehled objednávky.pdf"
    assert attachment.disposition == "attachment"


def test_mime_02_multipart_attachment_wrapper_is_unsupported() -> None:
    wrapped = multipart("1", "mixed", [text_part("1.0", "Must not become body")])
    wrapped["filename"] = "forwarded-message.mime"
    wrapped_headers = wrapped["headers"]
    assert isinstance(wrapped_headers, list)
    wrapped_headers.append(header("Content-Disposition", "attachment"))
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), wrapped]))

    assert_parse_error(message, "mime_unsupported_attachment_type")


def test_mime_02_decorative_inline_multipart_never_leaks_child_text() -> None:
    decorative = multipart("1", "related", [text_part("1.0", "Private child")])
    decorative_headers = decorative["headers"]
    assert isinstance(decorative_headers, list)
    decorative_headers.append(header("Content-Disposition", "inline"))
    message = gmail_message(
        multipart("", "mixed", [text_part("0", "Public body"), decorative])
    )

    parsed = parse_inbound_email(MAILBOX_KEY, message)

    assert parsed.text == "Public body"
    assert parsed.warnings == ("mime_ignored_decorative_inline",)


def test_mime_02_referenced_inline_multipart_is_unsupported() -> None:
    referenced = multipart("1", "related", [text_part("1.0", "Private child")])
    referenced_headers = referenced["headers"]
    assert isinstance(referenced_headers, list)
    referenced_headers.extend(
        [
            header("Content-Disposition", "inline"),
            header("Content-ID", "<bundle>"),
        ]
    )
    message = gmail_message(
        multipart(
            "",
            "related",
            [
                text_part(
                    "0",
                    '<p>Public body</p><object data="cid:bundle"></object>',
                    mime_type="text/html",
                ),
                referenced,
            ],
        )
    )

    assert_parse_error(message, "mime_unsupported_attachment_type")


def test_mime_02_opaque_inline_multipart_still_validates_its_children() -> None:
    private_child = text_part("1.0", "Private child")
    private_body = private_child["body"]
    assert isinstance(private_body, dict)
    private_body["data"] = "***private***"
    opaque = multipart("1", "related", [private_child])
    opaque_headers = opaque["headers"]
    assert isinstance(opaque_headers, list)
    opaque_headers.append(header("Content-Disposition", "inline"))
    message = gmail_message(
        multipart("", "mixed", [text_part("0", "Public body"), opaque])
    )

    assert_parse_error(message, "mime_malformed_base64url")


def test_mime_02_disposition_only_supported_part_uses_fallback_filename() -> None:
    png = synthetic_file("png")
    part = attachment_part(
        "1",
        "image/png",
        data=png,
        disposition="attachment",
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    attachment = parse_inbound_email(MAILBOX_KEY, message).attachments[0]

    assert attachment == Attachment(
        part_id="1",
        filename="attachment",
        media_family="image",
        media_type="image/png",
        disposition="attachment",
        content_id=None,
        size=len(png),
        data=png,
    )


def test_mime_02_referenced_filename_without_disposition_is_inline() -> None:
    png = synthetic_file("png")
    part = attachment_part(
        "1",
        "image/png",
        filename="chart.png",
        data=png,
        disposition=None,
        content_id="<chart>",
    )
    message = gmail_message(
        multipart(
            "",
            "related",
            [
                text_part(
                    "0",
                    '<p>Body</p><img src="cid:chart">',
                    mime_type="text/html",
                ),
                part,
            ],
        )
    )

    attachment = parse_inbound_email(MAILBOX_KEY, message).attachments[0]

    assert attachment.filename == "chart.png"
    assert attachment.disposition == "inline"


@pytest.mark.parametrize(
    ("declared_type", "actual_kind"),
    [
        ("application/pdf", "png"),
        ("audio/mpeg", "wav"),
        ("audio/wav", "jpeg"),
        ("audio/x-wav", "pdf"),
        ("image/jpeg", "png"),
        ("image/png", "mp3-id3"),
    ],
)
def test_mime_02_each_declared_type_rejects_a_different_supported_signature(
    declared_type: str,
    actual_kind: str,
) -> None:
    part = attachment_part(
        "1",
        declared_type,
        filename="mismatch.bin",
        data=synthetic_file(actual_kind),
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    assert_parse_error(message, "mime_attachment_type_mismatch")


def test_mime_02_file_bearing_unsupported_type_is_terminal() -> None:
    private_bytes = b"PK\x03\x04private unsupported bytes"
    part = attachment_part(
        "1",
        "application/zip",
        filename="archive.zip",
        data=private_bytes,
    )
    message = gmail_message(multipart("", "mixed", [text_part("0", "Body"), part]))

    error = assert_parse_error(message, "mime_unsupported_attachment_type")

    assert "private unsupported bytes" not in str(error)


@pytest.mark.parametrize(
    ("count", "expected_code"),
    [(4, None), (5, None), (6, "mime_too_many_attachments")],
)
def test_att_01_attachment_count_boundary(
    count: int,
    expected_code: str | None,
) -> None:
    contents = [synthetic_file("pdf") for _ in range(count)]
    if count == 6:
        contents[-1] = b"not a PDF signature"
    message, external = external_attachment_message(contents)

    if expected_code is not None:
        assert_parse_error(message, expected_code, external)
        return

    parsed = parse_inbound_email(
        MAILBOX_KEY,
        message,
        external_attachments=external,
    )
    assert len(parsed.attachments) == count
    assert tuple(item.part_id for item in parsed.attachments) == tuple(
        str(index) for index in range(1, count + 1)
    )


@pytest.mark.parametrize(
    ("size", "expected_code"),
    [
        (PER_ATTACHMENT_LIMIT - 1, None),
        (PER_ATTACHMENT_LIMIT, None),
        (PER_ATTACHMENT_LIMIT + 1, "mime_attachment_too_large"),
    ],
)
def test_att_01_decoded_per_attachment_size_boundary_ignores_reported_size(
    size: int,
    expected_code: str | None,
) -> None:
    content = synthetic_file("pdf", size)
    message, external = external_attachment_message([content])

    if expected_code is not None:
        assert_parse_error(message, expected_code, external)
        return

    attachment = parse_inbound_email(
        MAILBOX_KEY,
        message,
        external_attachments=external,
    ).attachments[0]
    assert attachment.size == size
    assert len(attachment.data) == size


@pytest.mark.parametrize(
    ("total_size", "expected_code"),
    [
        (TOTAL_ATTACHMENT_LIMIT - 1, None),
        (TOTAL_ATTACHMENT_LIMIT, None),
        (TOTAL_ATTACHMENT_LIMIT + 1, "mime_attachments_too_large"),
    ],
)
def test_att_01_decoded_total_size_boundary_ignores_reported_sizes(
    total_size: int,
    expected_code: str | None,
) -> None:
    first_size = 12 * MIB
    contents = [
        synthetic_file("pdf", first_size),
        synthetic_file("pdf", total_size - first_size),
    ]
    message, external = external_attachment_message(contents)

    if expected_code is not None:
        assert_parse_error(message, expected_code, external)
        return

    parsed = parse_inbound_email(
        MAILBOX_KEY,
        message,
        external_attachments=external,
    )
    assert sum(attachment.size for attachment in parsed.attachments) == total_size
