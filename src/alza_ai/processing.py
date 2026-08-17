import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from enum import StrEnum
from functools import partial
from time import monotonic
from typing import Protocol, TypeVar, cast
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1.transaction import Transaction

from alza_ai.attachments import AttachmentAnalysisError
from alza_ai.domain import Attachment, AttachmentInsight, GeneratedReply, InboundEmail
from alza_ai.gmail import (
    GmailAmbiguousSendError,
    GmailRetryableError,
    GmailTerminalError,
    OutboundMessage,
    SentMessage,
    ThreadSnapshot,
    build_threaded_reply,
    deterministic_outbound_message_id,
)
from alza_ai.mime import MimeParseError, parse_inbound_email
from alza_ai.reply_providers import (
    ReplyProviderError,
    RetryClassification,
)
from alza_ai.retries import BoundedRetry, RetryBudgetExceeded

PROCESSING_LEASE_SECONDS = 120
MAX_PROCESSING_ATTEMPTS = 5
PROCESSING_COLLECTION = "message-processing"
PROCESSING_DEADLINE_SECONDS = 105.0

_TELEMETRY_FIELDS = frozenset(
    {
        "event",
        "correlation_id",
        "mailbox_key",
        "message_id",
        "state",
        "stage",
        "attempt",
        "provider",
        "model",
        "retry_class",
        "error_code",
        "stage_latency_ms",
        "total_latency_ms",
    }
)
_TELEMETRY_INTEGER_FIELDS = frozenset(
    {"attempt", "stage_latency_ms", "total_latency_ms"}
)
_SAFE_TELEMETRY_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}")

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class ProcessingState(StrEnum):
    PROCESSING = "processing"
    SEND_PENDING = "send_pending"
    SENT = "sent"
    COMPLETED = "completed"
    TERMINAL_ERROR = "terminal_error"


class ClaimDisposition(StrEnum):
    OWNED = "owned"
    DUPLICATE = "duplicate"
    FINAL = "final"
    EXHAUSTED = "exhausted"


class ProcessResult(StrEnum):
    ACK = "ack"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class WorkItem:
    mailbox_key: str
    message_id: str


@dataclass(frozen=True, slots=True)
class SenderPolicy:
    mailbox_address: str
    allowed_senders: tuple[str, ...]

    def __post_init__(self) -> None:
        mailbox = _normalize_address(self.mailbox_address)
        senders = tuple(
            normalized
            for sender in self.allowed_senders
            if (normalized := _normalize_address(sender)) is not None
        )
        if mailbox is None or not senders or len(senders) != len(self.allowed_senders):
            raise ValueError("invalid sender policy")
        object.__setattr__(self, "mailbox_address", mailbox)
        object.__setattr__(self, "allowed_senders", tuple(dict.fromkeys(senders)))

    def rejection_code(self, inbound: InboundEmail) -> str | None:
        sender = _normalize_address(inbound.sender)
        if sender == self.mailbox_address or _is_automated(inbound):
            return "policy_reply_loop"
        if sender is None or sender not in self.allowed_senders:
            return "policy_sender_not_allowed"
        return None


def sanitize_telemetry(fields: Mapping[str, object]) -> dict[str, object]:
    event: dict[str, object] = {}
    for key, value in fields.items():
        if key not in _TELEMETRY_FIELDS:
            continue
        if key in _TELEMETRY_INTEGER_FIELDS:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                event[key] = value
            continue
        if isinstance(value, str) and _SAFE_TELEMETRY_TEXT.fullmatch(value) is not None:
            event[key] = value
    return event


def _log_telemetry(event: Mapping[str, object]) -> None:
    logger.info(json.dumps(event, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    disposition: ClaimDisposition
    record_id: str
    state: ProcessingState
    attempt_count: int
    thread_id: str | None = None
    outbound_message_id: str | None = None
    sent_message_id: str | None = None


class ProcessingStoreError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Snapshot(Protocol):
    exists: bool

    def to_dict(self) -> Mapping[str, object] | None: ...


class _Document(Protocol):
    def get(self, *, transaction: object) -> _Snapshot: ...


class _Collection(Protocol):
    def document(self, document_id: str) -> _Document: ...


class _Transaction(Protocol):
    def set(self, document: _Document, value: Mapping[str, object]) -> None: ...

    def update(self, document: _Document, value: Mapping[str, object]) -> None: ...


class _FirestoreClient(Protocol):
    def collection(self, name: str) -> _Collection: ...

    def transaction(self) -> _Transaction: ...


class _TransactionRunner(Protocol):
    def __call__(
        self,
        transaction: _Transaction,
        operation: Callable[[_Transaction], _T],
    ) -> _T: ...


def _firestore_transaction_runner[T](
    transaction: _Transaction,
    operation: Callable[[_Transaction], T],
) -> T:
    transactional = firestore.transactional(operation)
    return cast(T, transactional(cast(Transaction, transaction)))


class ProcessingStore:
    def __init__(
        self,
        client: object,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease_seconds: int = PROCESSING_LEASE_SECONDS,
        max_attempts: int = MAX_PROCESSING_ATTEMPTS,
        transaction_runner: object = _firestore_transaction_runner,
    ) -> None:
        self._client = cast(_FirestoreClient, client)
        self._clock = clock
        self._lease = timedelta(seconds=lease_seconds)
        self._max_attempts = max_attempts
        self._run = cast(_TransactionRunner, transaction_runner)

    def claim(self, work: WorkItem, lease_owner: str) -> ProcessingClaim:
        now = self._now()
        record_id = _record_id(work)
        document = self._client.collection(PROCESSING_COLLECTION).document(record_id)

        def claim_in_transaction(transaction: _Transaction) -> ProcessingClaim:
            snapshot = document.get(transaction=transaction)
            if not snapshot.exists:
                record: dict[str, object] = {
                    "mailbox_key": work.mailbox_key,
                    "message_id": work.message_id,
                    "thread_id": None,
                    "state": ProcessingState.PROCESSING.value,
                    "lease_owner": lease_owner,
                    "lease_expires_at": now + self._lease,
                    "attempt_count": 1,
                    "outbound_message_id": None,
                    "sent_message_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "retry_code": None,
                    "error_code": None,
                }
                transaction.set(document, record)
                return _claim(record_id, record, ClaimDisposition.OWNED)

            record = _record(snapshot)
            _matches_work(record, work)
            state = _state(record)
            if state in {ProcessingState.COMPLETED, ProcessingState.TERMINAL_ERROR}:
                return _claim(record_id, record, ClaimDisposition.FINAL)
            if _lease_active(record, now):
                return _claim(record_id, record, ClaimDisposition.DUPLICATE)

            attempt_count = _attempt_count(record)
            disposition = ClaimDisposition.OWNED
            if (
                attempt_count >= self._max_attempts
                and state is not ProcessingState.SENT
            ):
                disposition = ClaimDisposition.EXHAUSTED
            else:
                attempt_count = min(attempt_count + 1, self._max_attempts)
            updates = {
                "lease_owner": lease_owner,
                "lease_expires_at": now + self._lease,
                "attempt_count": attempt_count,
                "updated_at": now,
            }
            transaction.update(document, updates)
            record.update(updates)
            return _claim(record_id, record, disposition)

        return self._transaction(claim_in_transaction)

    def mark_send_pending(
        self,
        record_id: str,
        lease_owner: str,
        *,
        thread_id: str,
        outbound_message_id: str,
    ) -> None:
        self._transition(
            record_id,
            lease_owner,
            allowed={ProcessingState.PROCESSING},
            updates={
                "state": ProcessingState.SEND_PENDING.value,
                "thread_id": thread_id,
                "outbound_message_id": outbound_message_id,
                "retry_code": None,
            },
        )

    def mark_sent(
        self,
        record_id: str,
        lease_owner: str,
        *,
        sent_message_id: str,
    ) -> None:
        self._transition(
            record_id,
            lease_owner,
            allowed={ProcessingState.SEND_PENDING},
            updates={
                "state": ProcessingState.SENT.value,
                "sent_message_id": sent_message_id,
                "retry_code": None,
            },
        )

    def mark_completed(self, record_id: str, lease_owner: str) -> None:
        self._transition(
            record_id,
            lease_owner,
            allowed={ProcessingState.SENT},
            updates={
                "state": ProcessingState.COMPLETED.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "retry_code": None,
            },
        )

    def mark_terminal(
        self,
        record_id: str,
        lease_owner: str,
        *,
        error_code: str,
    ) -> None:
        self._transition(
            record_id,
            lease_owner,
            allowed={ProcessingState.PROCESSING, ProcessingState.SEND_PENDING},
            updates={
                "state": ProcessingState.TERMINAL_ERROR.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "retry_code": None,
                "error_code": error_code,
            },
        )

    def release_retry(
        self,
        record_id: str,
        lease_owner: str,
        *,
        retry_code: str,
    ) -> None:
        self._transition(
            record_id,
            lease_owner,
            allowed={
                ProcessingState.PROCESSING,
                ProcessingState.SEND_PENDING,
                ProcessingState.SENT,
            },
            updates={
                "lease_owner": None,
                "lease_expires_at": None,
                "retry_code": retry_code,
            },
        )

    def _transition(
        self,
        record_id: str,
        lease_owner: str,
        *,
        allowed: set[ProcessingState],
        updates: Mapping[str, object],
    ) -> None:
        now = self._now()
        document = self._client.collection(PROCESSING_COLLECTION).document(record_id)

        def transition(transaction: _Transaction) -> None:
            record = _record(document.get(transaction=transaction))
            _require_owner(record, lease_owner, now)
            if _state(record) not in allowed:
                raise ProcessingStoreError("processing_transition_invalid")
            transaction.update(document, {**updates, "updated_at": now})

        self._transaction(transition)

    def _transaction(self, operation: Callable[[_Transaction], _T]) -> _T:
        try:
            return self._run(self._client.transaction(), operation)
        except ProcessingStoreError:
            raise
        except Exception:  # noqa: BLE001 - sanitize Firestore boundary
            raise ProcessingStoreError("processing_store_unavailable") from None

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ProcessingStoreError("processing_clock_invalid")
        return now.astimezone(UTC)


class _GmailPort(Protocol):
    def get_message(self, message_id: str) -> Mapping[str, object]: ...

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes: ...

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None: ...

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot: ...

    def send_message(self, message: OutboundMessage) -> SentMessage: ...


class _Parser(Protocol):
    def __call__(
        self,
        mailbox_key: str,
        message: Mapping[str, object],
        external_attachments: Mapping[str, bytes],
    ) -> InboundEmail: ...


class _Analyzer(Protocol):
    async def analyze(
        self, attachments: Sequence[Attachment]
    ) -> tuple[AttachmentInsight, ...]: ...


class _Provider(Protocol):
    async def generate(
        self,
        *,
        current_text: str,
        attachment_insights: Sequence[AttachmentInsight],
    ) -> GeneratedReply: ...


class _TerminalProcessingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ProcessingDeadlineExceeded(Exception):
    pass


@dataclass(slots=True)
class _ProcessingTrace:
    work: WorkItem
    started: float
    deadline: float
    attempt: int | None = None
    state: ProcessingState = ProcessingState.PROCESSING
    retry_class: str = "none"
    error_code: str | None = None


class MessageCoordinator:
    def __init__(
        self,
        *,
        store: ProcessingStore,
        gmail: object,
        analyzer: object,
        provider: object,
        parser: object = parse_inbound_email,
        owner_factory: Callable[[], str] = lambda: uuid4().hex,
        after_send: Callable[[], None] | None = None,
        sender_policy: SenderPolicy | None = None,
        telemetry: Callable[[Mapping[str, object]], None] | None = _log_telemetry,
        monotonic: Callable[[], float] = monotonic,
        deadline_seconds: float = PROCESSING_DEADLINE_SECONDS,
        retry: BoundedRetry | None = None,
    ) -> None:
        if not 0 < deadline_seconds <= PROCESSING_DEADLINE_SECONDS:
            raise ValueError("invalid processing deadline")
        self._store = store
        self._gmail = cast(_GmailPort, gmail)
        self._parser = cast(_Parser, parser)
        self._analyzer = cast(_Analyzer, analyzer)
        self._provider = cast(_Provider, provider)
        self._owner_factory = owner_factory
        self._after_send = after_send
        self._sender_policy = sender_policy
        self._telemetry = telemetry
        self._monotonic = monotonic
        self._deadline_seconds = deadline_seconds
        self._bounded_retry = retry or BoundedRetry()

    async def process(self, work: WorkItem) -> ProcessResult:
        started = self._monotonic()
        trace = _ProcessingTrace(
            work=work,
            started=started,
            deadline=started + self._deadline_seconds,
        )
        try:
            result = await self._process(work, trace)
        except BaseException:
            self._emit(
                trace,
                event="processing_interrupted",
                stage="finished",
                state=trace.state.value,
                retry_class="retryable",
                error_code="processing_interrupted",
                total_latency_ms=self._elapsed_ms(trace.started),
            )
            raise
        self._emit(
            trace,
            event="processing_finished",
            stage="finished",
            state=trace.state.value,
            retry_class=trace.retry_class,
            error_code=trace.error_code,
            total_latency_ms=self._elapsed_ms(trace.started),
        )
        return result

    async def _process(
        self,
        work: WorkItem,
        trace: _ProcessingTrace,
    ) -> ProcessResult:
        owner = self._owner_factory()
        try:
            self._check_deadline(trace)
            claim = self._store.claim(work, owner)
        except ProcessingStoreError:
            self._set_failure(trace, "processing_store_unavailable", retryable=True)
            return ProcessResult.RETRY
        except _ProcessingDeadlineExceeded:
            self._set_failure(trace, "processing_deadline_exceeded", retryable=True)
            return ProcessResult.RETRY
        trace.attempt = claim.attempt_count
        trace.state = claim.state
        if claim.disposition is ClaimDisposition.FINAL:
            trace.state = claim.state
            return ProcessResult.ACK
        if claim.disposition is ClaimDisposition.DUPLICATE:
            self._set_failure(trace, "processing_lease_active", retryable=True)
            return ProcessResult.RETRY
        if claim.disposition is ClaimDisposition.EXHAUSTED:
            return await self._finish_exhausted(work, claim, owner, trace)

        send_may_have_happened = claim.state is ProcessingState.SEND_PENDING
        try:
            if claim.state is ProcessingState.SENT:
                return self._finish_success(work, claim.record_id, owner, trace)
            if claim.state is ProcessingState.SEND_PENDING:
                sent_message_id = await self._inspect_for_reply(work, claim, trace)
                if sent_message_id is not None:
                    self._check_deadline(trace)
                    self._store.mark_sent(
                        claim.record_id,
                        owner,
                        sent_message_id=sent_message_id,
                    )
                    trace.state = ProcessingState.SENT
                    return self._finish_success(work, claim.record_id, owner, trace)

            inbound = await self._load_inbound(work, trace)
            if self._sender_policy is not None:
                rejection_code = self._sender_policy.rejection_code(inbound)
                if rejection_code is not None:
                    raise _TerminalProcessingError(rejection_code)

            self._check_deadline(trace)
            attachment_started = self._monotonic()
            try:
                insights = await asyncio.wait_for(
                    self._analyzer.analyze(inbound.attachments),
                    timeout=self._remaining(trace),
                )
            except TimeoutError:
                raise _ProcessingDeadlineExceeded from None
            self._emit_stage(trace, "attachments", attachment_started)
            self._check_deadline(trace)

            provider_started = self._monotonic()
            try:
                reply = await asyncio.wait_for(
                    self._provider.generate(
                        current_text=inbound.text,
                        attachment_insights=insights,
                    ),
                    timeout=self._remaining(trace),
                )
            except TimeoutError:
                raise _ProcessingDeadlineExceeded from None
            self._emit_stage(
                trace,
                "provider",
                provider_started,
                provider=reply.provider,
                model=reply.model,
            )
            self._check_deadline(trace)
            outbound = build_threaded_reply(
                mailbox_key=work.mailbox_key,
                source_message_id=work.message_id,
                thread_id=inbound.thread_id,
                recipient=inbound.reply_to or inbound.sender,
                subject=inbound.subject,
                source_rfc_message_id=inbound.rfc_message_id,
                references=inbound.references,
                text=reply.text,
            )
            outbound_message_id = deterministic_outbound_message_id(
                work.mailbox_key, work.message_id
            )
            if claim.state is ProcessingState.SEND_PENDING and (
                claim.thread_id != inbound.thread_id
                or claim.outbound_message_id != outbound_message_id
            ):
                raise ProcessingStoreError("processing_record_invalid")
            if claim.state is ProcessingState.PROCESSING:
                self._check_deadline(trace)
                self._store.mark_send_pending(
                    claim.record_id,
                    owner,
                    thread_id=inbound.thread_id,
                    outbound_message_id=outbound_message_id,
                )
                trace.state = ProcessingState.SEND_PENDING
                send_may_have_happened = True
                recovery_claim = ProcessingClaim(
                    disposition=ClaimDisposition.OWNED,
                    record_id=claim.record_id,
                    state=ProcessingState.SEND_PENDING,
                    attempt_count=claim.attempt_count,
                    thread_id=inbound.thread_id,
                    outbound_message_id=outbound_message_id,
                )
                sent_message_id = await self._inspect_for_reply(
                    work, recovery_claim, trace
                )
                if sent_message_id is not None:
                    self._check_deadline(trace)
                    self._store.mark_sent(
                        claim.record_id,
                        owner,
                        sent_message_id=sent_message_id,
                    )
                    trace.state = ProcessingState.SENT
                    return self._finish_success(work, claim.record_id, owner, trace)

            self._check_deadline(trace)
            send_started = self._monotonic()
            sent = self._gmail.send_message(outbound)
            self._emit_stage(trace, "send", send_started)
            if self._after_send is not None:
                self._after_send()
            if sent.thread_id != inbound.thread_id:
                raise GmailAmbiguousSendError("gmail_invalid_response")
            self._check_deadline(trace)
            self._store.mark_sent(
                claim.record_id,
                owner,
                sent_message_id=sent.message_id,
            )
            trace.state = ProcessingState.SENT
            return self._finish_success(work, claim.record_id, owner, trace)
        except GmailAmbiguousSendError as error:
            self._set_failure(trace, error.code, retryable=True)
            return self._retry(claim.record_id, owner, error.code)
        except GmailRetryableError as error:
            self._set_failure(trace, error.code, retryable=True)
            return self._retry(claim.record_id, owner, error.code)
        except GmailTerminalError as error:
            if send_may_have_happened:
                self._set_failure(trace, error.code, retryable=True)
                return self._retry(claim.record_id, owner, error.code)
            return self._terminal(work, claim.record_id, owner, error.code, trace)
        except MimeParseError as error:
            return self._terminal(work, claim.record_id, owner, error.code, trace)
        except AttachmentAnalysisError as error:
            self._set_failure(trace, error.code, retryable=True)
            return self._retry(claim.record_id, owner, error.code)
        except ReplyProviderError as error:
            if error.classification is RetryClassification.RETRYABLE:
                self._set_failure(trace, error.code, retryable=True)
                return self._retry(claim.record_id, owner, error.code)
            return self._terminal(work, claim.record_id, owner, error.code, trace)
        except _TerminalProcessingError as error:
            return self._terminal(work, claim.record_id, owner, error.code, trace)
        except _ProcessingDeadlineExceeded:
            code = "processing_deadline_exceeded"
            self._set_failure(trace, code, retryable=True)
            return self._retry(claim.record_id, owner, code)
        except ProcessingStoreError:
            self._set_failure(trace, "processing_store_unavailable", retryable=True)
            return ProcessResult.RETRY

    async def _load_inbound(
        self,
        work: WorkItem,
        trace: _ProcessingTrace,
    ) -> InboundEmail:
        self._check_deadline(trace)
        started = self._monotonic()
        message = await self._gmail_read(
            lambda: self._gmail.get_message(work.message_id), trace
        )
        attachments: dict[str, bytes] = {}
        for attachment_id in _external_attachment_ids(message):
            attachments[attachment_id] = await self._gmail_read(
                partial(self._gmail.get_attachment, work.message_id, attachment_id),
                trace,
            )
        inbound = self._parser(work.mailbox_key, message, attachments)
        self._emit_stage(trace, "gmail_fetch", started)
        self._check_deadline(trace)
        if (
            inbound.mailbox_key != work.mailbox_key
            or inbound.message_id != work.message_id
        ):
            raise _TerminalProcessingError("processing_source_mismatch")
        return inbound

    async def _inspect_for_reply(
        self,
        work: WorkItem,
        claim: ProcessingClaim,
        trace: _ProcessingTrace,
    ) -> str | None:
        self._check_deadline(trace)
        if claim.thread_id is None or claim.outbound_message_id is None:
            raise ProcessingStoreError("processing_record_invalid")
        if claim.outbound_message_id != deterministic_outbound_message_id(
            work.mailbox_key, work.message_id
        ):
            raise ProcessingStoreError("processing_record_invalid")
        thread_id = claim.thread_id
        started = self._monotonic()
        thread = await self._gmail_read(
            lambda: self._gmail.inspect_thread(thread_id), trace
        )
        self._emit_stage(trace, "thread_inspection", started)
        self._check_deadline(trace)
        if thread.thread_id != thread_id:
            raise GmailRetryableError("gmail_invalid_response")
        for message in thread.messages:
            if (
                message.rfc_message_id == claim.outbound_message_id
                or message.source_message_id == work.message_id
            ):
                return message.message_id
        return None

    def _finish_success(
        self,
        work: WorkItem,
        record_id: str,
        owner: str,
        trace: _ProcessingTrace,
    ) -> ProcessResult:
        try:
            self._check_deadline(trace)
            label_started = self._monotonic()
            self._gmail.modify_labels(
                work.message_id,
                add=("AI/Processed",),
                remove=("UNREAD",),
            )
            self._emit_stage(trace, "labels", label_started)
            self._check_deadline(trace)
            state_started = self._monotonic()
            self._store.mark_completed(record_id, owner)
            self._emit_stage(trace, "state", state_started)
        except (GmailRetryableError, GmailTerminalError) as error:
            self._set_failure(trace, error.code, retryable=True)
            return self._retry(record_id, owner, error.code)
        except _ProcessingDeadlineExceeded:
            code = "processing_deadline_exceeded"
            self._set_failure(trace, code, retryable=True)
            return self._retry(record_id, owner, code)
        except ProcessingStoreError:
            self._set_failure(trace, "processing_store_unavailable", retryable=True)
            return ProcessResult.RETRY
        trace.state = ProcessingState.COMPLETED
        return ProcessResult.ACK

    async def _finish_exhausted(
        self,
        work: WorkItem,
        claim: ProcessingClaim,
        owner: str,
        trace: _ProcessingTrace,
    ) -> ProcessResult:
        if claim.state is ProcessingState.SEND_PENDING:
            try:
                sent_message_id = await self._inspect_for_reply(work, claim, trace)
                if sent_message_id is not None:
                    self._check_deadline(trace)
                    self._store.mark_sent(
                        claim.record_id,
                        owner,
                        sent_message_id=sent_message_id,
                    )
                    trace.state = ProcessingState.SENT
                    return self._finish_success(work, claim.record_id, owner, trace)
            except (
                GmailAmbiguousSendError,
                GmailRetryableError,
                GmailTerminalError,
            ) as error:
                self._set_failure(trace, error.code, retryable=True)
                return self._retry(claim.record_id, owner, error.code)
            except _ProcessingDeadlineExceeded:
                code = "processing_deadline_exceeded"
                self._set_failure(trace, code, retryable=True)
                return self._retry(claim.record_id, owner, code)
            except ProcessingStoreError:
                self._set_failure(trace, "processing_store_unavailable", retryable=True)
                return ProcessResult.RETRY
        return self._terminal(
            work,
            claim.record_id,
            owner,
            "processing_attempts_exhausted",
            trace,
        )

    def _terminal(
        self,
        work: WorkItem,
        record_id: str,
        owner: str,
        code: str,
        trace: _ProcessingTrace,
    ) -> ProcessResult:
        try:
            self._check_deadline(trace)
            label_started = self._monotonic()
            self._gmail.modify_labels(
                work.message_id,
                add=("AI/Error",),
                remove=(),
            )
            self._emit_stage(trace, "labels", label_started)
            self._check_deadline(trace)
            state_started = self._monotonic()
            self._store.mark_terminal(record_id, owner, error_code=code)
            self._emit_stage(trace, "state", state_started)
        except (GmailRetryableError, GmailTerminalError) as error:
            self._set_failure(trace, error.code, retryable=True)
            return self._retry(record_id, owner, error.code)
        except _ProcessingDeadlineExceeded:
            deadline_code = "processing_deadline_exceeded"
            self._set_failure(trace, deadline_code, retryable=True)
            return self._retry(record_id, owner, deadline_code)
        except ProcessingStoreError:
            self._set_failure(trace, "processing_store_unavailable", retryable=True)
            return ProcessResult.RETRY
        trace.state = ProcessingState.TERMINAL_ERROR
        self._set_failure(trace, code, retryable=False)
        return ProcessResult.ACK

    def _retry(self, record_id: str, owner: str, code: str) -> ProcessResult:
        try:
            self._store.release_retry(record_id, owner, retry_code=code)
        except ProcessingStoreError:
            pass
        return ProcessResult.RETRY

    def _check_deadline(self, trace: _ProcessingTrace) -> None:
        if self._monotonic() >= trace.deadline:
            raise _ProcessingDeadlineExceeded

    async def _gmail_read[T](
        self,
        operation: Callable[[], T],
        trace: _ProcessingTrace,
    ) -> T:
        async def invoke() -> T:
            return operation()

        try:
            return await self._bounded_retry.run(
                invoke,
                retry_if=lambda error: isinstance(error, GmailRetryableError),
                remaining_seconds=lambda: self._remaining(trace),
            )
        except RetryBudgetExceeded:
            raise _ProcessingDeadlineExceeded from None

    def _remaining(self, trace: _ProcessingTrace) -> float:
        remaining = trace.deadline - self._monotonic()
        if remaining <= 0:
            raise _ProcessingDeadlineExceeded
        return remaining

    def _elapsed_ms(self, started: float) -> int:
        return max(round((self._monotonic() - started) * 1_000), 0)

    def _emit_stage(
        self,
        trace: _ProcessingTrace,
        stage: str,
        started: float,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self._emit(
            trace,
            event="processing_stage",
            stage=stage,
            state=trace.state.value,
            provider=provider,
            model=model,
            stage_latency_ms=self._elapsed_ms(started),
        )

    def _emit(
        self,
        trace: _ProcessingTrace,
        **fields: object,
    ) -> None:
        if self._telemetry is None:
            return
        common: dict[str, object] = {
            "correlation_id": _correlation_id(trace.work),
            "mailbox_key": trace.work.mailbox_key,
            "message_id": trace.work.message_id,
        }
        if trace.attempt is not None:
            common["attempt"] = trace.attempt
        event = sanitize_telemetry({**common, **fields})
        try:
            self._telemetry(event)
        except Exception:  # noqa: BLE001 - telemetry cannot affect processing
            return

    @staticmethod
    def _set_failure(
        trace: _ProcessingTrace,
        code: str,
        *,
        retryable: bool,
    ) -> None:
        trace.error_code = code
        trace.retry_class = "retryable" if retryable else "terminal"


def parse_work_envelope(value: object) -> WorkItem | None:
    if not isinstance(value, Mapping):
        return None
    message = value.get("message")
    if not isinstance(message, Mapping):
        return None
    encoded = message.get("data")
    if not isinstance(encoded, str):
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
        work = json.loads(decoded)
    except binascii.Error, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(work, Mapping) or work.get("schema_version") != 1:
        return None
    mailbox_key = work.get("mailbox_key")
    message_id = work.get("message_id")
    if (
        not isinstance(mailbox_key, str)
        or not mailbox_key
        or not isinstance(message_id, str)
        or not message_id
    ):
        return None
    return WorkItem(mailbox_key=mailbox_key, message_id=message_id)


def _record_id(work: WorkItem) -> str:
    return hashlib.sha256(f"{work.mailbox_key}:{work.message_id}".encode()).hexdigest()


def _correlation_id(work: WorkItem) -> str:
    return hashlib.sha256(
        f"processing:{work.mailbox_key}:{work.message_id}".encode()
    ).hexdigest()


def _normalize_address(value: str) -> str | None:
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        return None
    _, address = parseaddr(value, strict=True)
    if not address or address.count("@") != 1:
        return None
    local, domain = address.rsplit("@", 1)
    if not local or not domain or any(character.isspace() for character in address):
        return None
    try:
        normalized_domain = domain.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    return f"{local.casefold()}@{normalized_domain}"


def _is_automated(inbound: InboundEmail) -> bool:
    auto_submitted = (inbound.auto_submitted or "").strip().casefold()
    precedence = (inbound.precedence or "").strip().casefold()
    auto_response_suppress = (inbound.auto_response_suppress or "").strip().casefold()
    return (
        auto_submitted not in {"", "no"}
        or precedence in {"bulk", "list", "junk"}
        or bool((inbound.list_id or "").strip())
        or auto_response_suppress not in {"", "none"}
    )


def _record(snapshot: _Snapshot) -> dict[str, object]:
    value = snapshot.to_dict()
    if not snapshot.exists or not isinstance(value, Mapping):
        raise ProcessingStoreError("processing_record_invalid")
    return dict(value)


def _matches_work(record: Mapping[str, object], work: WorkItem) -> None:
    if (
        record.get("mailbox_key") != work.mailbox_key
        or record.get("message_id") != work.message_id
    ):
        raise ProcessingStoreError("processing_record_invalid")


def _state(record: Mapping[str, object]) -> ProcessingState:
    try:
        return ProcessingState(cast(str, record.get("state")))
    except TypeError, ValueError:
        raise ProcessingStoreError("processing_record_invalid") from None


def _attempt_count(record: Mapping[str, object]) -> int:
    value = record.get("attempt_count")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProcessingStoreError("processing_record_invalid")
    return value


def _optional_string(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProcessingStoreError("processing_record_invalid")
    return value


def _claim(
    record_id: str,
    record: Mapping[str, object],
    disposition: ClaimDisposition,
) -> ProcessingClaim:
    return ProcessingClaim(
        disposition=disposition,
        record_id=record_id,
        state=_state(record),
        attempt_count=_attempt_count(record),
        thread_id=_optional_string(record, "thread_id"),
        outbound_message_id=_optional_string(record, "outbound_message_id"),
        sent_message_id=_optional_string(record, "sent_message_id"),
    )


def _lease_active(record: Mapping[str, object], now: datetime) -> bool:
    owner = record.get("lease_owner")
    expires_at = record.get("lease_expires_at")
    return (
        isinstance(owner, str)
        and bool(owner)
        and isinstance(expires_at, datetime)
        and expires_at > now
    )


def _require_owner(
    record: Mapping[str, object],
    lease_owner: str,
    now: datetime,
) -> None:
    if record.get("lease_owner") != lease_owner or not _lease_active(record, now):
        raise ProcessingStoreError("processing_lease_not_owned")


def _external_attachment_ids(message: Mapping[str, object]) -> tuple[str, ...]:
    identifiers: list[str] = []

    def visit(part: object) -> None:
        if not isinstance(part, Mapping):
            return
        body = part.get("body")
        if isinstance(body, Mapping):
            attachment_id = body.get("attachmentId")
            if isinstance(attachment_id, str) and attachment_id:
                identifiers.append(attachment_id)
        parts = part.get("parts")
        if isinstance(parts, list):
            for child in parts:
                visit(child)

    visit(message.get("payload"))
    return tuple(dict.fromkeys(identifiers))
