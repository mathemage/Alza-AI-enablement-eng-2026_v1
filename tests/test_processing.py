import asyncio
import base64
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from fastapi import FastAPI

from alza_ai.domain import Attachment, AttachmentInsight, GeneratedReply, InboundEmail
from alza_ai.gmail import (
    GmailAmbiguousSendError,
    GmailRetryableError,
    OutboundMessage,
    SentMessage,
    ThreadMessage,
    ThreadSnapshot,
)
from alza_ai.main import create_app
from alza_ai.mime import MimeParseError
from alza_ai.processing import (
    ClaimDisposition,
    MessageCoordinator,
    ProcessingState,
    ProcessingStore,
    ProcessingStoreError,
    ProcessResult,
    WorkItem,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
WORK = WorkItem(
    mailbox_key="mailbox-key",
    message_id="message-1",
    history_id="101",
    correlation_id=hashlib.sha256(b"mailbox-key:message-1:101").hexdigest(),
)
SOURCE = InboundEmail(
    mailbox_key=WORK.mailbox_key,
    message_id=WORK.message_id,
    thread_id="thread-1",
    rfc_message_id="<source@example.test>",
    subject="Private subject marker",
    sender="private-sender@example.test",
    reply_to="private-reply@example.test",
    references=("<root@example.test>",),
    received_at=NOW,
    text="Private body marker",
    attachments=(),
    warnings=(),
)
REPLY = GeneratedReply(
    text="Private generated reply marker",
    html="Private generated reply marker",
    citations=(),
    search_entry_point_html=None,
    provider="fake",
    model="fake-model",
    input_tokens=1,
    output_tokens=1,
    total_tokens=2,
    provider_latency_ms=1,
    total_latency_ms=2,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeSnapshot:
    def __init__(self, value: Mapping[str, object] | None) -> None:
        self.exists = value is not None
        self._value = value

    def to_dict(self) -> Mapping[str, object] | None:
        return None if self._value is None else dict(self._value)


class FakeDocument:
    def __init__(self, client: FakeFirestore, path: str) -> None:
        self._client = client
        self.path = path

    def get(self, *, transaction: object) -> FakeSnapshot:
        del transaction
        value = self._client.documents.get(self.path)
        return FakeSnapshot(value)


class FakeCollection:
    def __init__(self, client: FakeFirestore, name: str) -> None:
        self._client = client
        self._name = name

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self._client, f"{self._name}/{document_id}")


class FakeTransaction:
    def __init__(self, client: FakeFirestore) -> None:
        self._client = client

    def set(self, document: FakeDocument, value: Mapping[str, object]) -> None:
        self._client.documents[document.path] = dict(value)

    def update(self, document: FakeDocument, value: Mapping[str, object]) -> None:
        self._client.documents[document.path].update(value)


class FakeFirestore:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}
        self.transaction_count = 0
        self._lock = threading.Lock()

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def run_transaction(
        self,
        transaction: FakeTransaction,
        operation: Callable[[object], object],
    ) -> object:
        with self._lock:
            self.transaction_count += 1
            return operation(transaction)


def make_store(
    clock: MutableClock | None = None,
) -> tuple[ProcessingStore, FakeFirestore, MutableClock]:
    selected_clock = clock or MutableClock()
    client = FakeFirestore()
    store = ProcessingStore(
        client,
        clock=selected_clock,
        transaction_runner=client.run_transaction,
    )
    return store, client, selected_clock


def own(store: ProcessingStore, owner: str = "owner-1") -> str:
    claim = store.claim(WORK, owner)
    assert claim.disposition is ClaimDisposition.OWNED
    return claim.record_id


def test_proc_01_sequential_redelivery_stops_after_completion() -> None:
    store, client, _ = make_store()
    record_id = own(store)

    store.mark_send_pending(
        record_id,
        "owner-1",
        thread_id="thread-1",
        outbound_message_id="<outbound@example.test>",
    )
    store.mark_sent(record_id, "owner-1", sent_message_id="sent-1")
    store.mark_completed(record_id, "owner-1")
    redelivery = store.claim(WORK, "owner-2")

    assert redelivery.disposition is ClaimDisposition.FINAL
    assert redelivery.state is ProcessingState.COMPLETED
    record = next(iter(client.documents.values()))
    assert record["attempt_count"] == 1
    assert record["lease_owner"] is None


def test_proc_01_simultaneous_claims_have_exactly_one_owner() -> None:
    store, client, _ = make_store()
    barrier = threading.Barrier(2)

    def claim(owner: str) -> ClaimDisposition:
        barrier.wait()
        return store.claim(WORK, owner).disposition

    with ThreadPoolExecutor(max_workers=2) as pool:
        dispositions = tuple(pool.map(claim, ("owner-1", "owner-2")))

    assert sorted(dispositions) == [
        ClaimDisposition.DUPLICATE,
        ClaimDisposition.OWNED,
    ]
    assert next(iter(client.documents.values()))["attempt_count"] == 1


def test_proc_01_in_flight_duplicate_remains_retryable() -> None:
    store, _, _ = make_store()
    assert store.claim(WORK, "active-owner").disposition is ClaimDisposition.OWNED
    gmail = FakeGmail()
    coordinator, provider = make_coordinator(store=store, gmail=gmail)

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.RETRY
    assert provider.calls == 0
    assert gmail.inspections == 0
    assert gmail.send_calls == []


def test_proc_01_expired_lease_is_reclaimed_once() -> None:
    store, client, clock = make_store()
    first = store.claim(WORK, "owner-1")
    clock.now += timedelta(seconds=121)

    second = store.claim(WORK, "owner-2")
    duplicate = store.claim(WORK, "owner-3")

    assert first.disposition is ClaimDisposition.OWNED
    assert second.disposition is ClaimDisposition.OWNED
    assert second.attempt_count == 2
    assert duplicate.disposition is ClaimDisposition.DUPLICATE
    assert next(iter(client.documents.values()))["lease_owner"] == "owner-2"


def test_proc_01_retry_release_is_immediately_reclaimable() -> None:
    store, _, _ = make_store()
    record_id = own(store)

    store.release_retry(record_id, "owner-1", retry_code="gmail_unavailable")
    retry = store.claim(WORK, "owner-2")

    assert retry.disposition is ClaimDisposition.OWNED
    assert retry.attempt_count == 2
    assert retry.state is ProcessingState.PROCESSING


def test_proc_01_attempt_limit_becomes_owned_terminal_work() -> None:
    store, _, _ = make_store()
    for attempt in range(1, 6):
        owner = f"owner-{attempt}"
        claim = store.claim(WORK, owner)
        assert claim.disposition is ClaimDisposition.OWNED
        assert claim.attempt_count == attempt
        store.release_retry(claim.record_id, owner, retry_code="still_retryable")

    exhausted = store.claim(WORK, "owner-6")

    assert exhausted.disposition is ClaimDisposition.EXHAUSTED
    assert exhausted.attempt_count == 5


def test_state_01_stale_owner_and_illegal_transition_are_rejected() -> None:
    store, _, _ = make_store()
    record_id = own(store)

    with pytest.raises(ProcessingStoreError, match="processing_lease_not_owned"):
        store.mark_completed(record_id, "owner-2")
    with pytest.raises(ProcessingStoreError, match="processing_transition_invalid"):
        store.mark_completed(record_id, "owner-1")


class FakeGmail:
    def __init__(self) -> None:
        self.thread_messages: list[ThreadMessage] = []
        self.inspections = 0
        self.send_calls: list[OutboundMessage] = []
        self.label_calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        self.label_results: list[BaseException | None] = []
        self.send_results: list[SentMessage | BaseException] = [
            SentMessage("sent-1", SOURCE.thread_id)
        ]

    def get_message(self, message_id: str) -> Mapping[str, object]:
        assert message_id == WORK.message_id
        return {
            "id": message_id,
            "threadId": SOURCE.thread_id,
            "raw_marker": "Private raw email marker",
        }

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raise AssertionError((message_id, attachment_id))

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot:
        assert thread_id == SOURCE.thread_id
        self.inspections += 1
        return ThreadSnapshot(thread_id, tuple(self.thread_messages))

    def send_message(self, message: OutboundMessage) -> SentMessage:
        self.send_calls.append(message)
        result = self.send_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None:
        self.label_calls.append((message_id, add, remove))
        if self.label_results:
            result = self.label_results.pop(0)
            if result is not None:
                raise result


class FakeAnalyzer:
    async def analyze(self, attachments: object) -> tuple[()]:
        assert attachments == ()
        return ()


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: object) -> GeneratedReply:
        self.calls += 1
        assert kwargs == {
            "current_text": SOURCE.text,
            "attachment_insights": (),
        }
        return REPLY


def fake_parser(
    mailbox_key: str,
    message: Mapping[str, object],
    external_attachments: Mapping[str, bytes],
) -> InboundEmail:
    assert mailbox_key == WORK.mailbox_key
    assert message["raw_marker"] == "Private raw email marker"
    assert external_attachments == {}
    return SOURCE


def make_coordinator(
    *,
    store: ProcessingStore,
    gmail: FakeGmail,
    after_send: Callable[[], None] | None = None,
    parser: object = fake_parser,
) -> tuple[MessageCoordinator, FakeProvider]:
    provider = FakeProvider()
    return (
        MessageCoordinator(
            store=store,
            gmail=gmail,
            parser=parser,
            analyzer=FakeAnalyzer(),
            provider=provider,
            owner_factory=lambda: "coordinator-owner",
            after_send=after_send,
        ),
        provider,
    )


def outbound_identity(message: OutboundMessage) -> tuple[str, str]:
    parsed = BytesParser(policy=policy.default).parsebytes(message.raw)
    return str(parsed["Message-ID"]), str(parsed["X-Alza-AI-Source-Message-ID"])


def test_proc_03_ambiguous_send_recovers_by_thread_inspection() -> None:
    store, client, _ = make_store()
    gmail = FakeGmail()
    gmail.send_results = [GmailAmbiguousSendError("gmail_transport_error")]
    coordinator, _ = make_coordinator(store=store, gmail=gmail)

    first = asyncio.run(coordinator.process(WORK))
    message_id, source_id = outbound_identity(gmail.send_calls[0])
    gmail.thread_messages.append(ThreadMessage("sent-1", message_id, source_id))
    second = asyncio.run(coordinator.process(WORK))

    assert first is ProcessResult.RETRY
    assert second is ProcessResult.ACK
    assert len(gmail.send_calls) == 1
    assert gmail.inspections == 2
    assert gmail.label_calls == [(WORK.message_id, ("AI/Processed",), ("UNREAD",))]
    assert next(iter(client.documents.values()))["state"] == "completed"


def test_proc_03_unaccepted_ambiguous_send_retries_after_negative_inspection() -> None:
    store, _, _ = make_store()
    gmail = FakeGmail()
    gmail.send_results = [
        GmailAmbiguousSendError("gmail_transport_error"),
        SentMessage("sent-1", SOURCE.thread_id),
    ]
    coordinator, _ = make_coordinator(store=store, gmail=gmail)

    first = asyncio.run(coordinator.process(WORK))
    second = asyncio.run(coordinator.process(WORK))

    assert (first, second) == (ProcessResult.RETRY, ProcessResult.ACK)
    assert gmail.inspections == 2
    assert len(gmail.send_calls) == 2


class SimulatedCrash(BaseException):
    pass


def test_proc_03_crash_after_send_recovers_without_a_second_send() -> None:
    store, client, clock = make_store()
    gmail = FakeGmail()

    def crash() -> None:
        raise SimulatedCrash

    crashing, _ = make_coordinator(store=store, gmail=gmail, after_send=crash)
    with pytest.raises(SimulatedCrash):
        asyncio.run(crashing.process(WORK))

    record = next(iter(client.documents.values()))
    assert record["state"] == "send_pending"
    message_id, source_id = outbound_identity(gmail.send_calls[0])
    gmail.thread_messages.append(ThreadMessage("sent-1", message_id, source_id))
    clock.now += timedelta(seconds=121)
    recovered, _ = make_coordinator(store=store, gmail=gmail)

    assert asyncio.run(recovered.process(WORK)) is ProcessResult.ACK
    assert len(gmail.send_calls) == 1
    assert gmail.label_calls == [(WORK.message_id, ("AI/Processed",), ("UNREAD",))]
    assert next(iter(client.documents.values()))["state"] == "completed"


def test_proc_02_success_sends_threaded_identity_then_labels() -> None:
    store, _, _ = make_store()
    gmail = FakeGmail()
    coordinator, provider = make_coordinator(store=store, gmail=gmail)

    result = asyncio.run(coordinator.process(WORK))

    assert result is ProcessResult.ACK
    assert provider.calls == 1
    assert gmail.inspections == 1
    assert len(gmail.send_calls) == 1
    message = gmail.send_calls[0]
    parsed = BytesParser(policy=policy.default).parsebytes(message.raw)
    assert message.thread_id == SOURCE.thread_id
    assert parsed["To"] == SOURCE.reply_to
    assert parsed["Subject"] == SOURCE.subject
    assert parsed["In-Reply-To"] == SOURCE.rfc_message_id
    assert parsed["References"] == (f"{SOURCE.references[0]} {SOURCE.rfc_message_id}")
    assert parsed["X-Alza-AI-Source-Message-ID"] == WORK.message_id
    assert gmail.label_calls == [(WORK.message_id, ("AI/Processed",), ("UNREAD",))]


def test_label_01_label_retry_never_resends_confirmed_message() -> None:
    store, client, _ = make_store()
    gmail = FakeGmail()
    gmail.label_results = [GmailRetryableError("gmail_transport_error"), None]
    coordinator, provider = make_coordinator(store=store, gmail=gmail)

    first = asyncio.run(coordinator.process(WORK))
    second = asyncio.run(coordinator.process(WORK))

    assert (first, second) == (ProcessResult.RETRY, ProcessResult.ACK)
    assert provider.calls == 1
    assert len(gmail.send_calls) == 1
    assert gmail.inspections == 1
    assert gmail.label_calls == [
        (WORK.message_id, ("AI/Processed",), ("UNREAD",)),
        (WORK.message_id, ("AI/Processed",), ("UNREAD",)),
    ]
    assert next(iter(client.documents.values()))["state"] == "completed"


def test_priv_01_store_and_logs_never_receive_content_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, client, _ = make_store()
    gmail = FakeGmail()
    attachment = Attachment(
        part_id="private-part",
        filename="Private filename marker.pdf",
        media_family="document",
        media_type="application/pdf",
        disposition="attachment",
        content_id=None,
        size=31,
        data=b"Private attachment bytes marker",
    )
    insight = AttachmentInsight(
        filename=attachment.filename,
        media_type=attachment.media_type,
        summary="Private insight summary marker",
        extracted_text="Private extracted text or transcript marker",
        relevant_facts=("Private insight fact marker",),
        warnings=("Private insight warning marker",),
    )
    private_source = replace(SOURCE, attachments=(attachment,))

    def private_parser(*args: object) -> InboundEmail:
        del args
        return private_source

    class PrivateAnalyzer:
        async def analyze(self, attachments: object) -> tuple[AttachmentInsight, ...]:
            assert attachments == (attachment,)
            return (insight,)

    class PrivateProvider:
        async def generate(self, **kwargs: object) -> GeneratedReply:
            assert kwargs == {
                "current_text": private_source.text,
                "attachment_insights": (insight,),
            }
            return REPLY

    coordinator = MessageCoordinator(
        store=store,
        gmail=gmail,
        parser=private_parser,
        analyzer=PrivateAnalyzer(),
        provider=PrivateProvider(),
        owner_factory=lambda: "coordinator-owner",
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK

    persisted = json.dumps(client.documents, default=str)
    captured_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert SOURCE.reply_to is not None
    for private_marker in (
        "Private raw email marker",
        SOURCE.subject,
        SOURCE.sender,
        SOURCE.reply_to,
        SOURCE.text,
        REPLY.text,
        attachment.filename,
        "Private attachment bytes marker",
        insight.summary,
        insight.extracted_text,
        *insight.relevant_facts,
        *insight.warnings,
    ):
        assert private_marker not in persisted
        assert private_marker not in captured_logs
    assert set(next(iter(client.documents.values()))) == {
        "mailbox_key",
        "message_id",
        "thread_id",
        "state",
        "lease_owner",
        "lease_expires_at",
        "attempt_count",
        "outbound_message_id",
        "sent_message_id",
        "created_at",
        "updated_at",
        "retry_code",
        "error_code",
    }


def test_label_01_terminal_failure_adds_error_and_leaves_unread() -> None:
    store, client, _ = make_store()
    gmail = FakeGmail()

    def terminal_parser(*args: object) -> InboundEmail:
        del args
        raise MimeParseError("mime_malformed_message")

    coordinator, _ = make_coordinator(
        store=store,
        gmail=gmail,
        parser=terminal_parser,
    )

    assert asyncio.run(coordinator.process(WORK)) is ProcessResult.ACK
    assert gmail.send_calls == []
    assert gmail.label_calls == [(WORK.message_id, ("AI/Error",), ())]
    record = next(iter(client.documents.values()))
    assert record["state"] == "terminal_error"
    assert record["error_code"] == "mime_malformed_message"


class StubCoordinator:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.received: list[WorkItem] = []

    async def process(self, work: WorkItem) -> ProcessResult:
        self.received.append(work)
        return self.result


def envelope(value: object) -> dict[str, object]:
    data = base64.b64encode(json.dumps(value).encode()).decode()
    return {"message": {"data": data}}


async def post(app: FastAPI, payload: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post("/jobs/process-message", json=payload)


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [(ProcessResult.ACK, 204), (ProcessResult.RETRY, 503)],
)
def test_api_01_processing_route_maps_coordinator_result(
    result: ProcessResult,
    expected_status: int,
) -> None:
    coordinator = StubCoordinator(result)
    response = asyncio.run(
        post(
            create_app(processing_coordinator=coordinator),
            envelope(
                {
                    "schema_version": 1,
                    "mailbox_key": WORK.mailbox_key,
                    "message_id": WORK.message_id,
                    "history_id": WORK.history_id,
                    "correlation_id": WORK.correlation_id,
                }
            ),
        )
    )

    assert response.status_code == expected_status
    assert response.content == b""
    assert coordinator.received == [WORK]


def test_api_01_malformed_work_is_acknowledged_without_reflection() -> None:
    coordinator = StubCoordinator(ProcessResult.ACK)
    response = asyncio.run(
        post(
            create_app(processing_coordinator=coordinator),
            envelope({"schema_version": 2, "body": "Private HTTP marker"}),
        )
    )

    assert response.status_code == 204
    assert response.content == b""
    assert b"Private HTTP marker" not in response.content
    assert coordinator.received == []


def test_proc_03_asgi_redelivery_completes_once() -> None:
    store, _, _ = make_store()
    gmail = FakeGmail()
    coordinator, provider = make_coordinator(store=store, gmail=gmail)
    app = create_app(processing_coordinator=coordinator)
    payload = envelope(
        {
            "schema_version": 1,
            "mailbox_key": WORK.mailbox_key,
            "message_id": WORK.message_id,
            "history_id": WORK.history_id,
            "correlation_id": WORK.correlation_id,
        }
    )

    first = asyncio.run(post(app, payload))
    redelivery = asyncio.run(post(app, payload))

    assert (first.status_code, redelivery.status_code) == (204, 204)
    assert first.content == redelivery.content == b""
    assert provider.calls == 1
    assert len(gmail.send_calls) == 1
    assert gmail.label_calls == [(WORK.message_id, ("AI/Processed",), ("UNREAD",))]


def test_proc_03_asgi_ambiguous_acceptance_recovers_to_204() -> None:
    store, _, _ = make_store()
    gmail = FakeGmail()
    gmail.send_results = [GmailAmbiguousSendError("gmail_transport_error")]
    coordinator, _ = make_coordinator(store=store, gmail=gmail)
    app = create_app(processing_coordinator=coordinator)
    payload = envelope(
        {
            "schema_version": 1,
            "mailbox_key": WORK.mailbox_key,
            "message_id": WORK.message_id,
            "history_id": WORK.history_id,
            "correlation_id": WORK.correlation_id,
        }
    )

    first = asyncio.run(post(app, payload))
    message_id, source_id = outbound_identity(gmail.send_calls[0])
    gmail.thread_messages.append(ThreadMessage("sent-1", message_id, source_id))
    recovered = asyncio.run(post(app, payload))

    assert (first.status_code, recovered.status_code) == (503, 204)
    assert first.content == recovered.content == b""
    assert len(gmail.send_calls) == 1
