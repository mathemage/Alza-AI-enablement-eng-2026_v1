import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
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

PROCESSING_LEASE_SECONDS = 120
MAX_PROCESSING_ATTEMPTS = 5
PROCESSING_COLLECTION = "message-processing"

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
    ) -> None:
        self._store = store
        self._gmail = cast(_GmailPort, gmail)
        self._parser = cast(_Parser, parser)
        self._analyzer = cast(_Analyzer, analyzer)
        self._provider = cast(_Provider, provider)
        self._owner_factory = owner_factory
        self._after_send = after_send

    async def process(self, work: WorkItem) -> ProcessResult:
        owner = self._owner_factory()
        try:
            claim = self._store.claim(work, owner)
        except ProcessingStoreError:
            return ProcessResult.RETRY
        if claim.disposition is ClaimDisposition.FINAL:
            return ProcessResult.ACK
        if claim.disposition is ClaimDisposition.DUPLICATE:
            return ProcessResult.RETRY
        if claim.disposition is ClaimDisposition.EXHAUSTED:
            return self._finish_exhausted(work, claim, owner)

        send_may_have_happened = claim.state is ProcessingState.SEND_PENDING
        try:
            if claim.state is ProcessingState.SENT:
                return self._finish_success(work, claim.record_id, owner)
            if claim.state is ProcessingState.SEND_PENDING:
                sent_message_id = self._inspect_for_reply(work, claim)
                if sent_message_id is not None:
                    self._store.mark_sent(
                        claim.record_id,
                        owner,
                        sent_message_id=sent_message_id,
                    )
                    return self._finish_success(work, claim.record_id, owner)

            inbound = self._load_inbound(work)
            insights = await self._analyzer.analyze(inbound.attachments)
            reply = await self._provider.generate(
                current_text=inbound.text,
                attachment_insights=insights,
            )
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
                self._store.mark_send_pending(
                    claim.record_id,
                    owner,
                    thread_id=inbound.thread_id,
                    outbound_message_id=outbound_message_id,
                )
                send_may_have_happened = True
                recovery_claim = ProcessingClaim(
                    disposition=ClaimDisposition.OWNED,
                    record_id=claim.record_id,
                    state=ProcessingState.SEND_PENDING,
                    attempt_count=claim.attempt_count,
                    thread_id=inbound.thread_id,
                    outbound_message_id=outbound_message_id,
                )
                sent_message_id = self._inspect_for_reply(work, recovery_claim)
                if sent_message_id is not None:
                    self._store.mark_sent(
                        claim.record_id,
                        owner,
                        sent_message_id=sent_message_id,
                    )
                    return self._finish_success(work, claim.record_id, owner)

            sent = self._gmail.send_message(outbound)
            if self._after_send is not None:
                self._after_send()
            if sent.thread_id != inbound.thread_id:
                raise GmailAmbiguousSendError("gmail_invalid_response")
            self._store.mark_sent(
                claim.record_id,
                owner,
                sent_message_id=sent.message_id,
            )
            return self._finish_success(work, claim.record_id, owner)
        except GmailAmbiguousSendError as error:
            return self._retry(claim.record_id, owner, error.code)
        except GmailRetryableError as error:
            return self._retry(claim.record_id, owner, error.code)
        except GmailTerminalError as error:
            if send_may_have_happened:
                return self._retry(claim.record_id, owner, error.code)
            return self._terminal(work, claim.record_id, owner, error.code)
        except MimeParseError as error:
            return self._terminal(work, claim.record_id, owner, error.code)
        except AttachmentAnalysisError as error:
            return self._retry(claim.record_id, owner, error.code)
        except ReplyProviderError as error:
            if error.classification is RetryClassification.RETRYABLE:
                return self._retry(claim.record_id, owner, error.code)
            return self._terminal(work, claim.record_id, owner, error.code)
        except _TerminalProcessingError as error:
            return self._terminal(work, claim.record_id, owner, error.code)
        except ProcessingStoreError:
            return ProcessResult.RETRY

    def _load_inbound(self, work: WorkItem) -> InboundEmail:
        message = self._gmail.get_message(work.message_id)
        attachments = {
            attachment_id: self._gmail.get_attachment(work.message_id, attachment_id)
            for attachment_id in _external_attachment_ids(message)
        }
        inbound = self._parser(work.mailbox_key, message, attachments)
        if (
            inbound.mailbox_key != work.mailbox_key
            or inbound.message_id != work.message_id
        ):
            raise _TerminalProcessingError("processing_source_mismatch")
        return inbound

    def _inspect_for_reply(
        self,
        work: WorkItem,
        claim: ProcessingClaim,
    ) -> str | None:
        if claim.thread_id is None or claim.outbound_message_id is None:
            raise ProcessingStoreError("processing_record_invalid")
        if claim.outbound_message_id != deterministic_outbound_message_id(
            work.mailbox_key, work.message_id
        ):
            raise ProcessingStoreError("processing_record_invalid")
        thread = self._gmail.inspect_thread(claim.thread_id)
        if thread.thread_id != claim.thread_id:
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
    ) -> ProcessResult:
        try:
            self._gmail.modify_labels(
                work.message_id,
                add=("AI/Processed",),
                remove=("UNREAD",),
            )
            self._store.mark_completed(record_id, owner)
        except (GmailRetryableError, GmailTerminalError) as error:
            return self._retry(record_id, owner, error.code)
        except ProcessingStoreError:
            return ProcessResult.RETRY
        return ProcessResult.ACK

    def _finish_exhausted(
        self,
        work: WorkItem,
        claim: ProcessingClaim,
        owner: str,
    ) -> ProcessResult:
        if claim.state is ProcessingState.SEND_PENDING:
            try:
                sent_message_id = self._inspect_for_reply(work, claim)
                if sent_message_id is not None:
                    self._store.mark_sent(
                        claim.record_id,
                        owner,
                        sent_message_id=sent_message_id,
                    )
                    return self._finish_success(work, claim.record_id, owner)
            except (
                GmailAmbiguousSendError,
                GmailRetryableError,
                GmailTerminalError,
            ) as error:
                return self._retry(claim.record_id, owner, error.code)
            except ProcessingStoreError:
                return ProcessResult.RETRY
        return self._terminal(
            work,
            claim.record_id,
            owner,
            "processing_attempts_exhausted",
        )

    def _terminal(
        self,
        work: WorkItem,
        record_id: str,
        owner: str,
        code: str,
    ) -> ProcessResult:
        try:
            self._gmail.modify_labels(
                work.message_id,
                add=("AI/Error",),
                remove=(),
            )
            self._store.mark_terminal(record_id, owner, error_code=code)
        except (GmailRetryableError, GmailTerminalError) as error:
            return self._retry(record_id, owner, error.code)
        except ProcessingStoreError:
            return ProcessResult.RETRY
        return ProcessResult.ACK

    def _retry(self, record_id: str, owner: str, code: str) -> ProcessResult:
        try:
            self._store.release_retry(record_id, owner, retry_code=code)
        except ProcessingStoreError:
            pass
        return ProcessResult.RETRY


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
