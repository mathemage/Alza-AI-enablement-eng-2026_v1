import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, TypeVar, cast
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1.transaction import Transaction

from alza_ai.gmail import (
    GmailMessageMetadata,
    GmailMessageRef,
    GmailTerminalError,
    HistoryPage,
    MessagePage,
    WatchState,
)
from alza_ai.processing import PROCESSING_COLLECTION

MAILBOX_COLLECTION = "mailbox-synchronization"
SYNC_LEASE_SECONDS = 120
MAX_SYNC_PAGES = 10
MAX_SYNC_MESSAGES = 500

_T = TypeVar("_T")


class SyncResult(StrEnum):
    ACK = "ack"
    RETRY = "retry"


class LeaseDisposition(StrEnum):
    OWNED = "owned"
    BUSY = "busy"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class GmailPush:
    mailbox_address: str
    history_id: str


@dataclass(frozen=True, slots=True)
class WorkMetadata:
    mailbox_key: str
    message_id: str
    history_id: str
    correlation_id: str

    @classmethod
    def create(cls, mailbox_key: str, message_id: str, history_id: str) -> WorkMetadata:
        correlation_id = hashlib.sha256(
            f"{mailbox_key}:{message_id}:{history_id}".encode()
        ).hexdigest()
        return cls(mailbox_key, message_id, history_id, correlation_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mailbox_key": self.mailbox_key,
            "message_id": self.message_id,
            "history_id": self.history_id,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class MailboxLease:
    disposition: LeaseDisposition
    mailbox_key: str
    owner: str
    activated_at: datetime | None = None
    history_cursor: str | None = None
    history_page_token: str | None = None
    history_item_offset: int = 0
    reconciliation_page_token: str | None = None
    reconciliation_item_offset: int = 0


class SynchronizationStoreError(Exception):
    pass


class _Snapshot(Protocol):
    exists: bool

    def to_dict(self) -> Mapping[str, object] | None: ...


class _Document(Protocol):
    def get(self, *, transaction: object | None = None) -> _Snapshot: ...


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


class SynchronizationStore:
    def __init__(
        self,
        client: object,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease_seconds: int = SYNC_LEASE_SECONDS,
        transaction_runner: object = _firestore_transaction_runner,
    ) -> None:
        self._client = cast(_FirestoreClient, client)
        self._clock = clock
        self._lease = timedelta(seconds=lease_seconds)
        self._run = cast(_TransactionRunner, transaction_runner)

    def activate_or_renew(self, mailbox_key: str, watch: WatchState) -> bool:
        if not _valid_history_id(watch.history_id) or watch.expiration_ms <= 0:
            raise SynchronizationStoreError("synchronization_watch_invalid")
        now = self._now()
        document = self._mailbox_document(mailbox_key)

        def update_watch(transaction: _Transaction) -> bool:
            snapshot = document.get(transaction=transaction)
            if not snapshot.exists:
                transaction.set(
                    document,
                    {
                        "mailbox_key": mailbox_key,
                        "activated_at": now,
                        "history_cursor": watch.history_id,
                        "watch_history_id": watch.history_id,
                        "watch_expiration_ms": watch.expiration_ms,
                        "history_page_token": None,
                        "history_item_offset": 0,
                        "reconciliation_page_token": None,
                        "reconciliation_item_offset": 0,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "updated_at": now,
                    },
                )
                return True
            record = _mailbox_record(snapshot, mailbox_key)
            _validated_state(record)
            transaction.update(
                document,
                {
                    "watch_history_id": watch.history_id,
                    "watch_expiration_ms": watch.expiration_ms,
                    "updated_at": now,
                },
            )
            return False

        return self._transaction(update_watch)

    def acquire(self, mailbox_key: str, owner: str) -> MailboxLease:
        now = self._now()
        document = self._mailbox_document(mailbox_key)

        def acquire_lease(transaction: _Transaction) -> MailboxLease:
            snapshot = document.get(transaction=transaction)
            if not snapshot.exists:
                return MailboxLease(LeaseDisposition.INACTIVE, mailbox_key, owner)
            record = _mailbox_record(snapshot, mailbox_key)
            state = _validated_state(record)
            if _lease_active(record, now):
                return MailboxLease(LeaseDisposition.BUSY, mailbox_key, owner)
            transaction.update(
                document,
                {
                    "lease_owner": owner,
                    "lease_expires_at": now + self._lease,
                    "updated_at": now,
                },
            )
            return MailboxLease(
                LeaseDisposition.OWNED,
                mailbox_key,
                owner,
                activated_at=state.activated_at,
                history_cursor=state.history_cursor,
                history_page_token=state.history_page_token,
                history_item_offset=state.history_item_offset,
                reconciliation_page_token=state.reconciliation_page_token,
                reconciliation_item_offset=state.reconciliation_item_offset,
            )

        return self._transaction(acquire_lease)

    def save_history_checkpoint(
        self,
        mailbox_key: str,
        owner: str,
        expected_cursor: str,
        page_token: str | None,
        item_offset: int,
    ) -> None:
        self._update_owned(
            mailbox_key,
            owner,
            expected_cursor,
            {
                "history_page_token": page_token,
                "history_item_offset": item_offset,
            },
        )

    def commit_cursor(
        self,
        mailbox_key: str,
        owner: str,
        expected_cursor: str,
        history_cursor: str,
    ) -> None:
        if not _valid_history_id(history_cursor):
            raise SynchronizationStoreError("synchronization_history_invalid")
        self._update_owned(
            mailbox_key,
            owner,
            expected_cursor,
            {
                "history_cursor": history_cursor,
                "history_page_token": None,
                "history_item_offset": 0,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )

    def save_reconciliation_checkpoint(
        self,
        mailbox_key: str,
        owner: str,
        expected_cursor: str,
        page_token: str | None,
        item_offset: int,
    ) -> None:
        self._update_owned(
            mailbox_key,
            owner,
            expected_cursor,
            {
                "reconciliation_page_token": page_token,
                "reconciliation_item_offset": item_offset,
            },
        )

    def finish_reconciliation(
        self, mailbox_key: str, owner: str, expected_cursor: str
    ) -> None:
        self._update_owned(
            mailbox_key,
            owner,
            expected_cursor,
            {
                "reconciliation_page_token": None,
                "reconciliation_item_offset": 0,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )

    def replace_stale_cursor(
        self,
        mailbox_key: str,
        owner: str,
        expected_cursor: str,
        watch: WatchState,
    ) -> None:
        if not _valid_history_id(watch.history_id) or watch.expiration_ms <= 0:
            raise SynchronizationStoreError("synchronization_watch_invalid")
        self._update_owned(
            mailbox_key,
            owner,
            expected_cursor,
            {
                "history_cursor": watch.history_id,
                "watch_history_id": watch.history_id,
                "watch_expiration_ms": watch.expiration_ms,
                "history_page_token": None,
                "history_item_offset": 0,
                "reconciliation_page_token": None,
                "reconciliation_item_offset": 0,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )

    def release(self, mailbox_key: str, owner: str) -> None:
        now = self._now()
        document = self._mailbox_document(mailbox_key)

        def release_lease(transaction: _Transaction) -> None:
            record = _mailbox_record(document.get(transaction=transaction), mailbox_key)
            _require_owner(record, owner, now)
            transaction.update(
                document,
                {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                },
            )

        self._transaction(release_lease)

    def is_final(self, mailbox_key: str, message_id: str) -> bool:
        document_id = hashlib.sha256(f"{mailbox_key}:{message_id}".encode()).hexdigest()
        snapshot = (
            self._client.collection(PROCESSING_COLLECTION).document(document_id).get()
        )
        if not snapshot.exists:
            return False
        value = snapshot.to_dict()
        if not isinstance(value, Mapping):
            raise SynchronizationStoreError("synchronization_record_invalid")
        state = value.get("state")
        if not isinstance(state, str):
            raise SynchronizationStoreError("synchronization_record_invalid")
        return state in {"completed", "terminal_error"}

    def _update_owned(
        self,
        mailbox_key: str,
        owner: str,
        expected_cursor: str,
        updates: Mapping[str, object],
    ) -> None:
        now = self._now()
        document = self._mailbox_document(mailbox_key)

        def update(transaction: _Transaction) -> None:
            record = _mailbox_record(document.get(transaction=transaction), mailbox_key)
            _require_owner(record, owner, now)
            if record.get("history_cursor") != expected_cursor:
                raise SynchronizationStoreError("synchronization_cursor_changed")
            transaction.update(document, {**updates, "updated_at": now})

        self._transaction(update)

    def _mailbox_document(self, mailbox_key: str) -> _Document:
        document_id = hashlib.sha256(mailbox_key.encode()).hexdigest()
        return self._client.collection(MAILBOX_COLLECTION).document(document_id)

    def _transaction(self, operation: Callable[[_Transaction], _T]) -> _T:
        try:
            return self._run(self._client.transaction(), operation)
        except SynchronizationStoreError:
            raise
        except Exception:  # noqa: BLE001 - sanitize Firestore boundary
            raise SynchronizationStoreError(
                "synchronization_store_unavailable"
            ) from None

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise SynchronizationStoreError("synchronization_clock_invalid")
        return now.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _ValidatedState:
    activated_at: datetime
    history_cursor: str
    history_page_token: str | None
    history_item_offset: int
    reconciliation_page_token: str | None
    reconciliation_item_offset: int


def _validated_state(record: Mapping[str, object]) -> _ValidatedState:
    activated_at = record.get("activated_at")
    if not isinstance(activated_at, datetime) or activated_at.tzinfo is None:
        raise SynchronizationStoreError("synchronization_record_invalid")
    history_cursor = _decimal_string(record, "history_cursor")
    return _ValidatedState(
        activated_at=activated_at.astimezone(UTC),
        history_cursor=history_cursor,
        history_page_token=_optional_string(record, "history_page_token"),
        history_item_offset=_offset(record, "history_item_offset"),
        reconciliation_page_token=_optional_string(record, "reconciliation_page_token"),
        reconciliation_item_offset=_offset(record, "reconciliation_item_offset"),
    )


def _mailbox_record(snapshot: _Snapshot, mailbox_key: str) -> dict[str, object]:
    value = snapshot.to_dict()
    if (
        not snapshot.exists
        or not isinstance(value, Mapping)
        or value.get("mailbox_key") != mailbox_key
    ):
        raise SynchronizationStoreError("synchronization_record_invalid")
    return dict(value)


def _decimal_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not item
        or not item.isascii()
        or not item.isdigit()
    ):
        raise SynchronizationStoreError("synchronization_record_invalid")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise SynchronizationStoreError("synchronization_record_invalid")
    return item


def _offset(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise SynchronizationStoreError("synchronization_record_invalid")
    return item


def _lease_active(record: Mapping[str, object], now: datetime) -> bool:
    owner = record.get("lease_owner")
    expires_at = record.get("lease_expires_at")
    return (
        isinstance(owner, str)
        and bool(owner)
        and isinstance(expires_at, datetime)
        and expires_at > now
    )


def _require_owner(record: Mapping[str, object], owner: str, now: datetime) -> None:
    if record.get("lease_owner") != owner or not _lease_active(record, now):
        raise SynchronizationStoreError("synchronization_lease_not_owned")


class _PublishFuture(Protocol):
    def result(self) -> object: ...


class _PubSubClient(Protocol):
    def publish(self, topic: str, data: bytes) -> _PublishFuture: ...


class PubSubWorkPublisher:
    def __init__(self, client: object, topic: str) -> None:
        self._client = cast(_PubSubClient, client)
        self._topic = topic

    def publish(self, work: WorkMetadata) -> None:
        data = json.dumps(
            work.as_dict(), separators=(",", ":"), sort_keys=True
        ).encode()
        self._client.publish(self._topic, data=data).result()


class _GmailPort(Protocol):
    def start_watch(self, topic_name: str) -> WatchState: ...

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> HistoryPage: ...

    def list_unread(self, page_token: str | None = None) -> MessagePage: ...

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata: ...


class _WorkPublisher(Protocol):
    def publish(self, work: WorkMetadata) -> None: ...


@dataclass(frozen=True, slots=True)
class _HistoryCandidate:
    message_id: str
    history_id: str


class MailboxSynchronizer:
    def __init__(
        self,
        *,
        mailbox_key: str,
        mailbox_address: str,
        topic_name: str,
        store: SynchronizationStore,
        gmail: object,
        publisher: object,
        owner_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._mailbox_key = mailbox_key
        self._mailbox_address = mailbox_address.casefold()
        self._topic_name = topic_name
        self._store = store
        self._gmail = cast(_GmailPort, gmail)
        self._publisher = cast(_WorkPublisher, publisher)
        self._owner_factory = owner_factory

    def handle_push(self, push: GmailPush) -> SyncResult:
        if push.mailbox_address.casefold() != self._mailbox_address:
            return SyncResult.ACK
        owner = self._owner_factory()
        lease = self._acquire(owner)
        if isinstance(lease, SyncResult):
            return lease
        if lease.history_cursor is None:
            return self._fail(owner)
        if int(push.history_id) <= int(lease.history_cursor):
            try:
                self._store.release(self._mailbox_key, owner)
            except SynchronizationStoreError:
                return SyncResult.RETRY
            return SyncResult.ACK
        return self._synchronize(lease)

    def renew_watch(self) -> SyncResult:
        try:
            watch = self._gmail.start_watch(self._topic_name)
            activated = self._store.activate_or_renew(self._mailbox_key, watch)
        except Exception:  # noqa: BLE001 - sanitize adapter boundaries
            return SyncResult.RETRY
        if activated:
            return self.reconcile_unread()
        return SyncResult.ACK

    def reconcile_unread(self) -> SyncResult:
        owner = self._owner_factory()
        lease = self._acquire(owner)
        if isinstance(lease, SyncResult):
            return lease
        return self._reconcile_owned(lease, stale_cursor=False)

    def _acquire(self, owner: str) -> MailboxLease | SyncResult:
        try:
            lease = self._store.acquire(self._mailbox_key, owner)
        except SynchronizationStoreError:
            return SyncResult.RETRY
        if lease.disposition is LeaseDisposition.BUSY:
            return SyncResult.ACK
        if lease.disposition is LeaseDisposition.INACTIVE:
            return SyncResult.RETRY
        return lease

    def _synchronize(self, lease: MailboxLease) -> SyncResult:
        cursor = lease.history_cursor
        if cursor is None:
            return self._fail(lease.owner)
        page_token = lease.history_page_token
        item_offset = lease.history_item_offset
        pages = 0
        discovered = 0
        seen: set[str] = set()
        try:
            while pages < MAX_SYNC_PAGES:
                page = self._gmail.list_history(cursor, page_token)
                pages += 1
                candidates = _history_candidates(page)
                if item_offset > len(candidates):
                    raise SynchronizationStoreError(
                        "synchronization_checkpoint_invalid"
                    )
                remaining = MAX_SYNC_MESSAGES - discovered
                selected = candidates[item_offset : item_offset + remaining]
                self._publish_history(selected, seen)
                discovered += len(selected)
                next_offset = item_offset + len(selected)
                if next_offset < len(candidates):
                    self._store.save_history_checkpoint(
                        self._mailbox_key,
                        lease.owner,
                        cursor,
                        page_token,
                        next_offset,
                    )
                    self._store.release(self._mailbox_key, lease.owner)
                    return SyncResult.RETRY
                if page.next_page_token is None:
                    self._store.commit_cursor(
                        self._mailbox_key,
                        lease.owner,
                        cursor,
                        page.history_id,
                    )
                    return self.reconcile_unread()
                self._store.save_history_checkpoint(
                    self._mailbox_key,
                    lease.owner,
                    cursor,
                    page.next_page_token,
                    0,
                )
                if discovered >= MAX_SYNC_MESSAGES:
                    self._store.release(self._mailbox_key, lease.owner)
                    return SyncResult.RETRY
                page_token = page.next_page_token
                item_offset = 0
            self._store.release(self._mailbox_key, lease.owner)
            return SyncResult.RETRY
        except GmailTerminalError as error:
            if error.status == 404:
                return self._reconcile_owned(lease, stale_cursor=True)
            return self._fail(lease.owner)
        except Exception:  # noqa: BLE001 - sanitize adapter boundaries
            return self._fail(lease.owner)

    def _publish_history(
        self,
        candidates: tuple[_HistoryCandidate, ...],
        seen: set[str],
    ) -> None:
        for candidate in candidates:
            if candidate.message_id in seen:
                continue
            seen.add(candidate.message_id)
            if self._store.is_final(self._mailbox_key, candidate.message_id):
                continue
            self._publisher.publish(
                WorkMetadata.create(
                    self._mailbox_key,
                    candidate.message_id,
                    candidate.history_id,
                )
            )

    def _reconcile_owned(
        self, lease: MailboxLease, *, stale_cursor: bool
    ) -> SyncResult:
        if lease.history_cursor is None or lease.activated_at is None:
            return self._fail(lease.owner)
        page_token = lease.reconciliation_page_token
        item_offset = lease.reconciliation_item_offset
        pages = 0
        listed = 0
        try:
            while pages < MAX_SYNC_PAGES:
                page = self._gmail.list_unread(page_token)
                pages += 1
                if item_offset > len(page.messages):
                    raise SynchronizationStoreError(
                        "synchronization_checkpoint_invalid"
                    )
                remaining = MAX_SYNC_MESSAGES - listed
                selected = page.messages[item_offset : item_offset + remaining]
                for message in selected:
                    self._publish_reconciled(message, lease)
                listed += len(selected)
                next_offset = item_offset + len(selected)
                if next_offset < len(page.messages):
                    self._store.save_reconciliation_checkpoint(
                        self._mailbox_key,
                        lease.owner,
                        lease.history_cursor,
                        page_token,
                        next_offset,
                    )
                    self._store.release(self._mailbox_key, lease.owner)
                    return SyncResult.RETRY
                if page.next_page_token is None:
                    if stale_cursor:
                        watch = self._gmail.start_watch(self._topic_name)
                        self._store.replace_stale_cursor(
                            self._mailbox_key,
                            lease.owner,
                            lease.history_cursor,
                            watch,
                        )
                    else:
                        self._store.finish_reconciliation(
                            self._mailbox_key,
                            lease.owner,
                            lease.history_cursor,
                        )
                    return SyncResult.ACK
                self._store.save_reconciliation_checkpoint(
                    self._mailbox_key,
                    lease.owner,
                    lease.history_cursor,
                    page.next_page_token,
                    0,
                )
                if listed >= MAX_SYNC_MESSAGES:
                    self._store.release(self._mailbox_key, lease.owner)
                    return SyncResult.RETRY
                page_token = page.next_page_token
                item_offset = 0
            self._store.release(self._mailbox_key, lease.owner)
            return SyncResult.RETRY
        except Exception:  # noqa: BLE001 - sanitize adapter boundaries
            return self._fail(lease.owner)

    def _publish_reconciled(
        self, message: GmailMessageRef, lease: MailboxLease
    ) -> None:
        if self._store.is_final(self._mailbox_key, message.message_id):
            return
        metadata = self._gmail.get_message_metadata(message.message_id)
        if (
            metadata.message_id != message.message_id
            or metadata.thread_id != message.thread_id
        ):
            raise SynchronizationStoreError("synchronization_message_mismatch")
        if not {"INBOX", "UNREAD"}.issubset(metadata.label_ids):
            return
        try:
            received_at = datetime.fromtimestamp(
                metadata.internal_date_ms / 1000, tz=UTC
            )
        except OSError, OverflowError, ValueError:
            raise SynchronizationStoreError(
                "synchronization_message_metadata_invalid"
            ) from None
        if lease.activated_at is None or received_at < lease.activated_at:
            return
        if lease.history_cursor is None:
            raise SynchronizationStoreError("synchronization_record_invalid")
        self._publisher.publish(
            WorkMetadata.create(
                self._mailbox_key,
                message.message_id,
                lease.history_cursor,
            )
        )

    def _fail(self, owner: str) -> SyncResult:
        try:
            self._store.release(self._mailbox_key, owner)
        except SynchronizationStoreError:
            pass
        return SyncResult.RETRY


def parse_gmail_push_envelope(value: object) -> GmailPush | None:
    if not isinstance(value, Mapping):
        return None
    message = value.get("message")
    if not isinstance(message, Mapping):
        return None
    encoded = message.get("data")
    if not isinstance(encoded, str):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        notification = json.loads(decoded)
    except binascii.Error, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(notification, Mapping):
        return None
    mailbox_address = notification.get("emailAddress")
    raw_history_id = notification.get("historyId")
    history_id: object
    if (
        isinstance(raw_history_id, int)
        and not isinstance(raw_history_id, bool)
        and raw_history_id > 0
    ):
        history_id = str(raw_history_id)
    else:
        history_id = raw_history_id
    if (
        not isinstance(mailbox_address, str)
        or not mailbox_address
        or not isinstance(history_id, str)
        or not history_id
        or not history_id.isascii()
        or not history_id.isdigit()
    ):
        return None
    return GmailPush(mailbox_address, history_id)


def _history_candidates(page: HistoryPage) -> tuple[_HistoryCandidate, ...]:
    if not _valid_history_id(page.history_id):
        raise SynchronizationStoreError("synchronization_history_invalid")
    candidates: list[_HistoryCandidate] = []
    for record in page.records:
        history_id = _history_string(record, "id")
        if not _valid_history_id(history_id):
            raise SynchronizationStoreError("synchronization_history_invalid")
        for addition in _history_mappings(record, "messagesAdded"):
            message = _history_mapping(addition, "message")
            labels = _history_labels(message, "labelIds")
            if {"INBOX", "UNREAD"}.issubset(labels):
                candidates.append(
                    _HistoryCandidate(_history_string(message, "id"), history_id)
                )
        for addition in _history_mappings(record, "labelsAdded"):
            added_labels = _history_labels(addition, "labelIds")
            message = _history_mapping(addition, "message")
            current_labels = _history_labels(message, "labelIds")
            if "INBOX" in added_labels and {"INBOX", "UNREAD"}.issubset(current_labels):
                candidates.append(
                    _HistoryCandidate(_history_string(message, "id"), history_id)
                )
    return tuple(candidates)


def _history_mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise SynchronizationStoreError("synchronization_history_invalid")
    return item


def _history_mappings(
    value: Mapping[str, object], key: str
) -> tuple[Mapping[str, object], ...]:
    items = value.get(key, [])
    if not isinstance(items, list) or not all(
        isinstance(item, Mapping) for item in items
    ):
        raise SynchronizationStoreError("synchronization_history_invalid")
    return tuple(cast(Mapping[str, object], item) for item in items)


def _history_labels(value: Mapping[str, object], key: str) -> frozenset[str]:
    items = value.get(key, [])
    if not isinstance(items, list) or not all(
        isinstance(item, str) and item for item in items
    ):
        raise SynchronizationStoreError("synchronization_history_invalid")
    return frozenset(cast(str, item) for item in items)


def _history_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise SynchronizationStoreError("synchronization_history_invalid")
    return item


def _valid_history_id(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit()
