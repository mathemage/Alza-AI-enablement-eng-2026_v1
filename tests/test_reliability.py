import asyncio
import base64
import importlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic

import httpx
import pytest

import alza_ai.reply_providers as providers
from alza_ai import processing
from alza_ai.attachments import AttachmentAnalysisError
from alza_ai.domain import GeneratedReply, InboundEmail
from alza_ai.gmail import (
    GmailAmbiguousSendError,
    GmailRetryableError,
    GmailTerminalError,
    OutboundMessage,
    SentMessage,
    ThreadSnapshot,
)
from alza_ai.main import create_app
from alza_ai.mime import MimeParseError, parse_inbound_email
from alza_ai.processing import (
    ClaimDisposition,
    MessageCoordinator,
    ProcessingClaim,
    ProcessingState,
    ProcessingStoreError,
    ProcessResult,
    WorkItem,
)
from alza_ai.reply_providers import ReplyProviderError, RetryClassification

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
WORK = WorkItem(mailbox_key="opaque-mailbox", message_id="opaque-message")
SOURCE = InboundEmail(
    mailbox_key=WORK.mailbox_key,
    message_id=WORK.message_id,
    thread_id="opaque-thread",
    rfc_message_id="<source@example.test>",
    subject="PRIVATE SUBJECT",
    sender="Allowed Person <allowed@example.test>",
    reply_to=None,
    references=(),
    received_at=NOW,
    text="PRIVATE BODY",
    attachments=(),
    warnings=(),
)
REPLY = GeneratedReply(
    text="PRIVATE REPLY",
    html="PRIVATE REPLY",
    citations=(),
    search_entry_point_html=None,
    provider="fake-provider",
    model="fake-model",
    input_tokens=31,
    output_tokens=17,
    total_tokens=48,
    provider_latency_ms=7,
    total_latency_ms=9,
)


class FakeStore:
    def __init__(self) -> None:
        self.state = ProcessingState.PROCESSING
        self.retries: list[str] = []
        self.terminal_codes: list[str] = []
        self.completed = False
        self.claim_error: ProcessingStoreError | None = None
        self.terminal_error: ProcessingStoreError | None = None

    def claim(self, work: WorkItem, owner: str) -> ProcessingClaim:
        assert work == WORK
        assert owner == "owner"
        if self.claim_error is not None:
            raise self.claim_error
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
        self.completed = True
        self.state = ProcessingState.COMPLETED

    def mark_terminal(self, record_id: str, owner: str, *, error_code: str) -> None:
        del record_id, owner
        if self.terminal_error is not None:
            raise self.terminal_error
        self.terminal_codes.append(error_code)
        self.state = ProcessingState.TERMINAL_ERROR

    def release_retry(self, record_id: str, owner: str, *, retry_code: str) -> None:
        del record_id, owner
        self.retries.append(retry_code)


class FakeGmail:
    def __init__(self) -> None:
        self.get_error: BaseException | None = None
        self.send_error: BaseException | None = None
        self.label_error: BaseException | None = None
        self.send_calls: list[OutboundMessage] = []
        self.label_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def get_message(self, message_id: str) -> Mapping[str, object]:
        assert message_id == WORK.message_id
        if self.get_error is not None:
            raise self.get_error
        return {"id": message_id, "private": "PRIVATE RAW MESSAGE"}

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raise AssertionError((message_id, attachment_id))

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot:
        assert thread_id == SOURCE.thread_id
        return ThreadSnapshot(thread_id, ())

    def send_message(self, message: OutboundMessage) -> SentMessage:
        self.send_calls.append(message)
        if self.send_error is not None:
            raise self.send_error
        return SentMessage("opaque-sent", SOURCE.thread_id)

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None:
        assert message_id == WORK.message_id
        self.label_calls.append((add, remove))
        if self.label_error is not None:
            raise self.label_error


class FakeAnalyzer:
    def __init__(self, error: AttachmentAnalysisError | None = None) -> None:
        self.error = error
        self.calls = 0

    async def analyze(self, attachments: object) -> tuple[()]:
        assert attachments == ()
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ()


class FakeProvider:
    def __init__(self, error: ReplyProviderError | None = None) -> None:
        self.error = error
        self.calls = 0

    async def generate(self, **kwargs: object) -> GeneratedReply:
        assert kwargs == {
            "current_text": SOURCE.text,
            "attachment_insights": (),
        }
        self.calls += 1
        if self.error is not None:
            raise self.error
        return REPLY


class MutableMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def parse_source(*args: object) -> InboundEmail:
    del args
    return SOURCE


def make_coordinator(
    *,
    store: FakeStore | None = None,
    gmail: FakeGmail | None = None,
    analyzer: FakeAnalyzer | None = None,
    provider: FakeProvider | None = None,
    parser: object = parse_source,
    sender_policy: processing.SenderPolicy | None = None,
    telemetry: Callable[[Mapping[str, object]], None] | None = None,
    selected_monotonic: Callable[[], float] = monotonic,
) -> tuple[MessageCoordinator, FakeStore, FakeGmail, FakeAnalyzer, FakeProvider]:
    selected_store = store or FakeStore()
    selected_gmail = gmail or FakeGmail()
    selected_analyzer = analyzer or FakeAnalyzer()
    selected_provider = provider or FakeProvider()
    coordinator = MessageCoordinator(
        store=selected_store,  # type: ignore[arg-type]
        gmail=selected_gmail,
        analyzer=selected_analyzer,
        provider=selected_provider,
        parser=parser,
        owner_factory=lambda: "owner",
        sender_policy=sender_policy,
        telemetry=telemetry,
        monotonic=selected_monotonic,
    )
    return (
        coordinator,
        selected_store,
        selected_gmail,
        selected_analyzer,
        selected_provider,
    )


@pytest.mark.parametrize(
    ("boundary", "expected_code"),
    (
        ("gmail", "gmail_transport_error"),
        ("storage", "attachment_storage_unavailable"),
        ("provider", "reply_provider_unavailable"),
        ("store", "processing_store_unavailable"),
    ),
)
def test_fail_01_every_transient_boundary_remains_retryable(
    boundary: str, expected_code: str
) -> None:
    store = FakeStore()
    gmail = FakeGmail()
    analyzer = FakeAnalyzer()
    provider = FakeProvider()
    if boundary == "gmail":
        gmail.get_error = GmailRetryableError(expected_code)
    elif boundary == "storage":
        analyzer.error = AttachmentAnalysisError(expected_code)
    elif boundary == "provider":
        provider.error = ReplyProviderError(
            expected_code, RetryClassification.RETRYABLE
        )
    else:
        store.claim_error = ProcessingStoreError(expected_code)
    coordinator, _, _, _, _ = make_coordinator(
        store=store, gmail=gmail, analyzer=analyzer, provider=provider
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.RETRY
    if boundary == "store":
        assert store.retries == []
    else:
        assert store.retries == [expected_code]


def test_fail_01_ambiguous_send_is_retryable_and_never_acknowledged() -> None:
    gmail = FakeGmail()
    gmail.send_error = GmailAmbiguousSendError("gmail_transport_error")
    coordinator, store, _, _, _ = make_coordinator(gmail=gmail)

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.RETRY
    assert store.retries == ["gmail_transport_error"]
    assert len(gmail.send_calls) == 1


def test_fail_03_retry_is_two_attempts_with_one_full_jitter_delay() -> None:
    retry_module = importlib.import_module("alza_ai.retries")
    delays: list[float] = []
    calls = 0

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async def unavailable() -> None:
        nonlocal calls
        calls += 1
        raise GmailRetryableError("gmail_transport_error")

    retry = retry_module.BoundedRetry(random=lambda: 1.0, sleeper=sleeper)
    with pytest.raises(GmailRetryableError, match="gmail_transport_error"):
        asyncio.run(
            retry.run(
                unavailable,
                retry_if=lambda error: isinstance(error, GmailRetryableError),
            )
        )

    assert calls == 2
    assert delays == [0.25]


def test_time_01_retry_delay_that_exceeds_budget_starts_no_second_call() -> None:
    retry_module = importlib.import_module("alza_ai.retries")
    delays: list[float] = []
    calls = 0

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async def unavailable() -> None:
        nonlocal calls
        calls += 1
        raise GmailRetryableError("gmail_transport_error")

    retry = retry_module.BoundedRetry(random=lambda: 1.0, sleeper=sleeper)
    with pytest.raises(retry_module.RetryBudgetExceeded):
        asyncio.run(
            retry.run(
                unavailable,
                retry_if=lambda error: isinstance(error, GmailRetryableError),
                remaining_seconds=lambda: 0.1,
            )
        )

    assert calls == 1
    assert delays == []


def test_fail_03_coordinator_retries_only_the_safe_gmail_read() -> None:
    retry_module = importlib.import_module("alza_ai.retries")
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    class FlakyReadGmail(FakeGmail):
        def __init__(self) -> None:
            super().__init__()
            self.get_calls = 0

        def get_message(self, message_id: str) -> Mapping[str, object]:
            self.get_calls += 1
            if self.get_calls == 1:
                raise GmailRetryableError("gmail_transport_error")
            return super().get_message(message_id)

    gmail = FlakyReadGmail()
    store = FakeStore()
    retry = retry_module.BoundedRetry(random=lambda: 0.5, sleeper=sleeper)
    coordinator = MessageCoordinator(
        store=store,  # type: ignore[arg-type]
        gmail=gmail,
        analyzer=FakeAnalyzer(),
        provider=FakeProvider(),
        parser=parse_source,
        owner_factory=lambda: "owner",
        retry=retry,
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK
    assert gmail.get_calls == 2
    assert delays == [0.125]
    assert len(gmail.send_calls) == 1


@pytest.mark.parametrize("terminal_kind", ("mime", "provider"))
def test_fail_02_terminal_failure_acknowledges_only_after_label_and_state(
    terminal_kind: str,
) -> None:
    parser: object = parse_source
    provider = FakeProvider()
    expected_code = "reply_provider_invalid_response"
    if terminal_kind == "mime":
        expected_code = "mime_malformed_message"

        def terminal_parser(*args: object) -> InboundEmail:
            del args
            raise MimeParseError(expected_code)

        parser = terminal_parser
    else:
        provider.error = ReplyProviderError(expected_code, RetryClassification.TERMINAL)
    coordinator, store, gmail, _, _ = make_coordinator(parser=parser, provider=provider)

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK
    assert gmail.label_calls == [(("AI/Error",), ())]
    assert store.terminal_codes == [expected_code]
    assert store.state is ProcessingState.TERMINAL_ERROR


@pytest.mark.parametrize(
    "label_error",
    (
        GmailRetryableError("gmail_transport_error"),
        GmailTerminalError("gmail_forbidden", 403),
    ),
)
def test_label_01_terminal_label_failure_returns_retry_without_terminal_state(
    label_error: BaseException,
) -> None:
    gmail = FakeGmail()
    gmail.label_error = label_error

    def terminal_parser(*args: object) -> InboundEmail:
        del args
        raise MimeParseError("mime_malformed_message")

    coordinator, store, _, _, _ = make_coordinator(gmail=gmail, parser=terminal_parser)

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.RETRY
    assert gmail.label_calls == [(("AI/Error",), ())]
    assert store.terminal_codes == []
    assert store.state is ProcessingState.PROCESSING


def test_label_01_terminal_store_failure_returns_retry_after_error_label() -> None:
    store = FakeStore()
    store.terminal_error = ProcessingStoreError("processing_store_unavailable")

    def terminal_parser(*args: object) -> InboundEmail:
        del args
        raise MimeParseError("mime_malformed_message")

    coordinator, _, gmail, _, _ = make_coordinator(store=store, parser=terminal_parser)

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.RETRY
    assert gmail.label_calls == [(("AI/Error",), ())]
    assert store.terminal_codes == []
    assert store.state is ProcessingState.PROCESSING


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "javascript:alert(1)",
        "data:text/html,private",
        "https://user:secret@example.com/private",
        "https://127.0.0.1/private",
        "https://metadata.google.internal/private",
        "https://example.com/line\nbreak",
    ),
)
def test_sec_01_unsafe_citation_urls_are_discarded(unsafe_url: str) -> None:
    raw = providers._RawCitation(unsafe_url, "PRIVATE CITATION TITLE")

    assert providers._normalize_citations((raw,), "fake") == ()


def test_sec_01_reply_html_escapes_prose_url_and_citation_title() -> None:
    raw = providers._RawCitation(
        'https://example.com/path?a=1&b="quoted"',
        '<img src=x onerror="PRIVATE HTML"> & title',
    )
    citations = providers._normalize_citations((raw,), "fake")

    _, rendered, retained = providers._reply_alternatives(
        '<script>alert("PRIVATE HTML")</script> & prose', citations
    )

    assert retained == citations
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;img src=x onerror=&quot;PRIVATE HTML&quot;&gt;" in rendered
    assert 'href="https://example.com/path?a=1&amp;b=&quot;quoted&quot;"' in rendered


@pytest.mark.parametrize(
    ("source", "expected_code"),
    (
        (
            replace(SOURCE, sender="outsider@example.test"),
            "policy_sender_not_allowed",
        ),
        (replace(SOURCE, sender="assistant@example.test"), "policy_reply_loop"),
        (replace(SOURCE, auto_submitted="auto-replied"), "policy_reply_loop"),
        (replace(SOURCE, precedence="bulk"), "policy_reply_loop"),
        (
            replace(SOURCE, list_id="private-list.example.test"),
            "policy_reply_loop",
        ),
        (replace(SOURCE, auto_response_suppress="All"), "policy_reply_loop"),
    ),
)
def test_sec_01_sender_and_loop_policy_rejects_before_model_or_send(
    source: InboundEmail, expected_code: str
) -> None:
    def selected_parser(*args: object) -> InboundEmail:
        del args
        return source

    policy = processing.SenderPolicy(
        mailbox_address="assistant@example.test",
        allowed_senders=("allowed@example.test",),
    )
    coordinator, store, gmail, analyzer, provider = make_coordinator(
        parser=selected_parser,
        sender_policy=policy,
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK
    assert store.terminal_codes == [expected_code]
    assert gmail.label_calls == [(("AI/Error",), ())]
    assert analyzer.calls == 0
    assert provider.calls == 0
    assert gmail.send_calls == []


def test_sec_01_sender_allowlist_accepts_one_case_normalized_address() -> None:
    policy = processing.SenderPolicy(
        mailbox_address="Assistant@Example.Test",
        allowed_senders=("ALLOWED@EXAMPLE.TEST",),
    )
    coordinator, store, gmail, analyzer, provider = make_coordinator(
        sender_policy=policy
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK
    assert store.completed
    assert analyzer.calls == provider.calls == 1
    assert len(gmail.send_calls) == 1


def test_sec_01_mime_parser_preserves_loop_control_headers() -> None:
    encoded_body = base64.urlsafe_b64encode(b"Hello").decode().rstrip("=")
    message = {
        "id": WORK.message_id,
        "threadId": SOURCE.thread_id,
        "internalDate": "1786968000000",
        "payload": {
            "partId": "",
            "mimeType": "text/plain",
            "filename": "",
            "headers": [
                {"name": "Message-ID", "value": "<source@example.test>"},
                {"name": "From", "value": "allowed@example.test"},
                {"name": "Auto-Submitted", "value": "auto-replied"},
                {"name": "Precedence", "value": "bulk"},
                {"name": "List-Id", "value": "private-list.example.test"},
                {"name": "X-Auto-Response-Suppress", "value": "All"},
                {"name": "Content-Type", "value": "text/plain; charset=utf-8"},
            ],
            "body": {"data": encoded_body, "size": 5},
        },
    }

    inbound = parse_inbound_email(WORK.mailbox_key, message)

    assert inbound.auto_submitted == "auto-replied"
    assert inbound.precedence == "bulk"
    assert inbound.list_id == "private-list.example.test"
    assert inbound.auto_response_suppress == "All"


def test_obs_01_telemetry_allowlist_redacts_arbitrary_content_and_secrets() -> None:
    marker = "PRIVATE TELEMETRY MARKER"
    event = processing.sanitize_telemetry(
        {
            "event": "processing_finished",
            "mailbox_key": WORK.mailbox_key,
            "message_id": WORK.message_id,
            "stage": "finished",
            "retry_class": "terminal",
            "error_code": "policy_reply_loop",
            "total_latency_ms": 12,
            "sender": marker,
            "subject": marker,
            "body": marker,
            "prompt": marker,
            "reply": marker,
            "insight": marker,
            "filename": marker,
            "media": marker,
            "token": marker,
            "secret": marker,
            "exception": marker,
        }
    )

    serialized = json.dumps(event)
    assert marker not in serialized
    assert set(event) == {
        "event",
        "mailbox_key",
        "message_id",
        "stage",
        "retry_class",
        "error_code",
        "total_latency_ms",
    }


def test_obs_01_success_records_provider_stage_and_final_total_latency() -> None:
    events: list[Mapping[str, object]] = []
    clock = MutableMonotonic()

    class AdvancingProvider(FakeProvider):
        async def generate(self, **kwargs: object) -> GeneratedReply:
            result = await super().generate(**kwargs)
            clock.now += 0.125
            return result

    coordinator, _, _, _, _ = make_coordinator(
        provider=AdvancingProvider(),
        selected_monotonic=clock,
        telemetry=events.append,
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK
    provider_event = next(event for event in events if event.get("stage") == "provider")
    final_event = events[-1]
    assert provider_event["provider"] == REPLY.provider
    assert provider_event["model"] == REPLY.model
    assert provider_event["stage_latency_ms"] == 125
    assert final_event["event"] == "processing_finished"
    assert final_event["state"] == "completed"
    assert final_event["total_latency_ms"] == 125
    assert all(isinstance(event.get("stage_latency_ms", 0), int) for event in events)
    assert not any(
        marker in json.dumps(events)
        for marker in (SOURCE.subject, SOURCE.sender, SOURCE.text, REPLY.text)
    )


def test_obs_01_default_structured_logs_are_allowlisted_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    with caplog.at_level(logging.INFO, logger="alza_ai.processing"):
        coordinator = MessageCoordinator(
            store=store,  # type: ignore[arg-type]
            gmail=FakeGmail(),
            analyzer=FakeAnalyzer(),
            provider=FakeProvider(),
            parser=parse_source,
            owner_factory=lambda: "owner",
        )
        assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK

    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert events[-1]["event"] == "processing_finished"
    assert events[-1]["state"] == "completed"
    assert all(set(event) <= processing._TELEMETRY_FIELDS for event in events)
    assert not any(
        marker in caplog.text
        for marker in (SOURCE.subject, SOURCE.sender, SOURCE.text, REPLY.text)
    )


def test_time_01_deadline_stops_before_provider_or_send_and_records_retry() -> None:
    clock = MutableMonotonic()
    events: list[Mapping[str, object]] = []

    def deadline_parser(*args: object) -> InboundEmail:
        del args
        clock.now = 105.0
        return SOURCE

    coordinator, store, gmail, analyzer, provider = make_coordinator(
        parser=deadline_parser,
        selected_monotonic=clock,
        telemetry=events.append,
    )

    assert processing.PROCESSING_DEADLINE_SECONDS == 105.0
    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.RETRY
    assert store.retries == ["processing_deadline_exceeded"]
    assert analyzer.calls == 0
    assert provider.calls == 0
    assert gmail.send_calls == []
    assert events[-1]["retry_class"] == "retryable"
    assert events[-1]["error_code"] == "processing_deadline_exceeded"
    assert events[-1]["total_latency_ms"] == 105_000


def _work_envelope() -> dict[str, object]:
    data = base64.b64encode(
        json.dumps(
            {
                "schema_version": 1,
                "mailbox_key": WORK.mailbox_key,
                "message_id": WORK.message_id,
            }
        ).encode()
    ).decode()
    return {"message": {"data": data}}


async def _post_processing(coordinator: MessageCoordinator) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(processing_coordinator=coordinator))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post("/jobs/process-message", json=_work_envelope())


def test_api_01_retry_and_terminal_policy_have_safe_acknowledgments() -> None:
    retry_gmail = FakeGmail()
    retry_gmail.get_error = GmailRetryableError("gmail_transport_error")
    retry_coordinator, retry_store, _, _, _ = make_coordinator(gmail=retry_gmail)

    policy = processing.SenderPolicy(
        mailbox_address="assistant@example.test",
        allowed_senders=("allowed@example.test",),
    )
    terminal_coordinator, terminal_store, terminal_gmail, _, _ = make_coordinator(
        parser=lambda *args: replace(SOURCE, sender="outsider@example.test"),
        sender_policy=policy,
    )

    retry_response = asyncio.run(_post_processing(retry_coordinator))
    terminal_response = asyncio.run(_post_processing(terminal_coordinator))

    assert (retry_response.status_code, retry_response.content) == (503, b"")
    assert retry_store.retries == ["gmail_transport_error"]
    assert (terminal_response.status_code, terminal_response.content) == (204, b"")
    assert terminal_store.terminal_codes == ["policy_sender_not_allowed"]
    assert terminal_gmail.label_calls == [(("AI/Error",), ())]
