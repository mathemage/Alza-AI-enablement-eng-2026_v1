import base64
import json
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from alza_ai.attachments import (
    AttachmentAnalyzer,
    GeminiAttachmentResult,
)
from alza_ai.domain import Citation, GeneratedReply, InboundEmail
from alza_ai.gmail import OutboundMessage, SentMessage, ThreadSnapshot
from alza_ai.main import create_app
from alza_ai.mime import parse_inbound_email
from alza_ai.processing import (
    ClaimDisposition,
    MessageCoordinator,
    ProcessingClaim,
    ProcessingState,
    WorkItem,
)
from alza_ai.synchronization import WorkMetadata

NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)
AUTHENTICATED_HEADERS = {
    "authorization": "Bearer synthetic-oidc-token",
    "x-goog-authenticated-user-email": "serviceAccount:email-work-push@example.test",
}


class FakeStore:
    def __init__(self) -> None:
        self.state = ProcessingState.PROCESSING

    def claim(self, work: WorkItem, owner: str) -> ProcessingClaim:
        del work, owner
        return ProcessingClaim(
            disposition=ClaimDisposition.OWNED,
            record_id="opaque-record",
            state=self.state,
            attempt_count=1,
        )

    def mark_send_pending(
        self,
        record_id: str,
        owner: str,
        *,
        thread_id: str,
        outbound_message_id: str,
    ) -> None:
        del record_id, owner, thread_id, outbound_message_id
        self.state = ProcessingState.SEND_PENDING

    def mark_sent(self, record_id: str, owner: str, *, sent_message_id: str) -> None:
        del record_id, owner, sent_message_id
        self.state = ProcessingState.SENT

    def mark_completed(self, record_id: str, owner: str) -> None:
        del record_id, owner
        self.state = ProcessingState.COMPLETED

    def mark_terminal(self, record_id: str, owner: str, *, error_code: str) -> None:
        del record_id, owner, error_code
        self.state = ProcessingState.TERMINAL_ERROR

    def release_retry(self, record_id: str, owner: str, *, retry_code: str) -> None:
        del record_id, owner, retry_code


class FakeGmail:
    def __init__(self, message: Mapping[str, object] | None = None) -> None:
        self.message = message
        self.sent: list[OutboundMessage] = []
        self.labels: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def get_message(self, message_id: str) -> Mapping[str, object]:
        return self.message or {"id": message_id}

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raise AssertionError((message_id, attachment_id))

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot:
        return ThreadSnapshot(thread_id, ())

    def send_message(self, message: OutboundMessage) -> SentMessage:
        self.sent.append(message)
        return SentMessage("opaque-sent", message.thread_id)

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None:
        del message_id
        self.labels.append((add, remove))


class FakeAnalyzer:
    async def analyze(self, attachments: object) -> tuple[()]:
        assert attachments == ()
        return ()


class FakeProvider:
    async def generate(self, **kwargs: object) -> GeneratedReply:
        assert kwargs == {
            "current_text": "PRIVATE BODY",
            "attachment_insights": (),
        }
        return GeneratedReply(
            text="PRIVATE REPLY",
            html="PRIVATE REPLY",
            citations=(),
            search_entry_point_html=None,
            provider="fake",
            model="fake-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            provider_latency_ms=1,
            total_latency_ms=1,
        )


class FakeScratch:
    region = "europe-west3"
    bucket_name = "fake-scratch"

    def __init__(self) -> None:
        self.staged: list[tuple[str, bytes, str]] = []
        self.deleted: list[str] = []

    async def stage(self, *, object_name: str, data: bytes, media_type: str) -> str:
        self.staged.append((object_name, data, media_type))
        return f"gs://fake-scratch/{object_name}"

    async def delete(self, *, object_name: str) -> None:
        self.deleted.append(object_name)


class FakeAttachmentModel:
    def __init__(self) -> None:
        self.media_types: list[str] = []

    async def analyze(self, *, gcs_uri: str, media_type: str) -> GeminiAttachmentResult:
        assert gcs_uri.startswith("gs://fake-scratch/")
        self.media_types.append(media_type)
        return GeminiAttachmentResult(
            summary="PRIVATE INSIGHT",
            extracted_text="PRIVATE EXTRACTED TEXT",
            relevant_facts=("PRIVATE FACT",),
            warnings=(),
        )


class GroundedProvider:
    async def generate(self, **kwargs: object) -> GeneratedReply:
        assert kwargs["current_text"] == "PRIVATE BODY"
        return GeneratedReply(
            text="Grounded <unsafe>\n\nSources:\n[1] Safe: https://example.test/source",
            html=(
                "Grounded &lt;unsafe&gt;<br><br>Sources:<br>"
                '<a href="https://example.test/source">[1] Safe</a>'
            ),
            citations=(Citation("https://example.test/source", "Safe", "fake"),),
            search_entry_point_html="<div>PRIVATE SEARCH WIDGET</div>",
            provider="fake",
            model="fake-grounded-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            provider_latency_ms=1,
            total_latency_ms=1,
        )


def parse_source(
    mailbox_key: str,
    message: Mapping[str, object],
    external_attachments: Mapping[str, bytes],
) -> InboundEmail:
    assert external_attachments == {}
    return InboundEmail(
        mailbox_key=mailbox_key,
        message_id=str(message["id"]),
        thread_id="opaque-thread",
        rfc_message_id="<source@example.test>",
        subject="PRIVATE SUBJECT",
        sender="allowed@example.test",
        reply_to=None,
        references=(),
        received_at=NOW,
        text="PRIVATE BODY",
        attachments=(),
        warnings=(),
    )


def pubsub_envelope(value: Mapping[str, object]) -> dict[str, object]:
    data = base64.b64encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "opaque-delivery"}}


def gmail_message(media_type: str | None, content: bytes | None) -> dict[str, object]:
    def encoded(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    text = b"PRIVATE BODY"
    parts: list[dict[str, object]] = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "filename": "",
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
            "body": {"size": len(text), "data": encoded(text)},
        }
    ]
    if media_type is not None and content is not None:
        parts.append(
            {
                "partId": "1",
                "mimeType": media_type,
                "filename": "PRIVATE-FILENAME",
                "headers": [
                    {"name": "Content-Type", "value": media_type},
                    {"name": "Content-Disposition", "value": "attachment"},
                ],
                "body": {"size": len(content), "data": encoded(content)},
            }
        )
    return {
        "id": "opaque-message",
        "threadId": "opaque-thread",
        "internalDate": "1787047200000",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [
                {"name": "Message-ID", "value": "<source@example.test>"},
                {"name": "Subject", "value": "PRIVATE SUBJECT"},
                {"name": "From", "value": "allowed@example.test"},
                {"name": "References", "value": "<root@example.test>"},
                {"name": "Content-Type", "value": "multipart/mixed"},
            ],
            "body": {"size": 0},
            "parts": parts,
        },
    }


@contextmanager
def running_uvicorn(application: FastAPI) -> Iterator[str]:
    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    port = int(server_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(application, log_level="warning", access_log=False)
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [server_socket]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("uvicorn failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        server_socket.close()
        if thread.is_alive():
            raise RuntimeError("uvicorn failed to stop")


def test_published_correlation_survives_live_work_delivery() -> None:
    work = WorkMetadata.create("opaque-mailbox", "opaque-message", "101")
    events: list[Mapping[str, object]] = []
    coordinator = MessageCoordinator(
        store=FakeStore(),  # type: ignore[arg-type]
        gmail=FakeGmail(),
        analyzer=FakeAnalyzer(),
        provider=FakeProvider(),
        parser=parse_source,
        owner_factory=lambda: "opaque-owner",
        telemetry=events.append,
    )

    with running_uvicorn(create_app(processing_coordinator=coordinator)) as base_url:
        response = httpx.post(
            f"{base_url}/jobs/process-message",
            json=pubsub_envelope(work.as_dict()),
            headers=AUTHENTICATED_HEADERS,
            timeout=5,
        )

    assert (response.status_code, response.content) == (204, b"")
    assert events
    assert {event["correlation_id"] for event in events} == {work.correlation_id}
    serialized = json.dumps([work.as_dict(), *events], default=str)
    assert not any(
        marker in serialized
        for marker in ("PRIVATE BODY", "PRIVATE SUBJECT", "PRIVATE REPLY")
    )


def test_invalid_correlation_is_acknowledged_without_reflection_or_work() -> None:
    events: list[Mapping[str, object]] = []
    coordinator = MessageCoordinator(
        store=FakeStore(),  # type: ignore[arg-type]
        gmail=FakeGmail(),
        analyzer=FakeAnalyzer(),
        provider=FakeProvider(),
        parser=parse_source,
        owner_factory=lambda: "opaque-owner",
        telemetry=events.append,
    )
    malformed = WorkMetadata.create("opaque-mailbox", "opaque-message", "101").as_dict()
    malformed["correlation_id"] = "PRIVATE ATTACKER CORRELATION"

    with running_uvicorn(create_app(processing_coordinator=coordinator)) as base_url:
        response = httpx.post(
            f"{base_url}/jobs/process-message",
            json=pubsub_envelope(malformed),
            headers=AUTHENTICATED_HEADERS,
            timeout=5,
        )

    assert (response.status_code, response.content) == (204, b"")
    assert b"PRIVATE ATTACKER CORRELATION" not in response.content
    assert events == []


@pytest.mark.parametrize(
    ("media_type", "content"),
    (
        (None, None),
        ("application/pdf", b"%PDF-1.7\nowned"),
        ("audio/mpeg", b"ID3\x04\x00\x00\x00\x00\x00\x00owned"),
        ("audio/wav", b"RIFF\x10\x00\x00\x00WAVEowned"),
        ("image/jpeg", b"\xff\xd8\xff\xe0owned"),
        ("image/png", b"\x89PNG\r\n\x1a\nowned"),
    ),
)
def test_supported_messages_complete_over_live_http(
    media_type: str | None, content: bytes | None
) -> None:
    work = WorkMetadata.create("opaque-mailbox", "opaque-message", "101")
    gmail = FakeGmail(gmail_message(media_type, content))
    scratch = FakeScratch()
    model = FakeAttachmentModel()
    coordinator = MessageCoordinator(
        store=FakeStore(),  # type: ignore[arg-type]
        gmail=gmail,
        analyzer=AttachmentAnalyzer(scratch, model),
        provider=GroundedProvider(),
        parser=parse_inbound_email,
        owner_factory=lambda: "opaque-owner",
        telemetry=None,
    )

    with running_uvicorn(create_app(processing_coordinator=coordinator)) as base_url:
        response = httpx.post(
            f"{base_url}/jobs/process-message",
            json=pubsub_envelope(work.as_dict()),
            headers=AUTHENTICATED_HEADERS,
            timeout=5,
        )

    assert (response.status_code, response.content) == (204, b"")
    assert gmail.labels == [(("AI/Processed",), ("UNREAD",))]
    assert len(gmail.sent) == 1
    outbound = BytesParser(policy=policy.default).parsebytes(gmail.sent[0].raw)
    assert outbound["In-Reply-To"] == "<source@example.test>"
    assert outbound["References"] == "<root@example.test> <source@example.test>"
    html_part = outbound.get_body(preferencelist=("html",))
    assert html_part is not None
    html = html_part.get_content()
    assert "Grounded &lt;unsafe&gt;" in html
    assert 'href="https://example.test/source"' in html
    assert "<unsafe>" not in html
    expected_media = [] if media_type is None else [media_type]
    assert model.media_types == expected_media
    assert [item[2] for item in scratch.staged] == expected_media
    assert scratch.deleted == [item[0] for item in scratch.staged]
