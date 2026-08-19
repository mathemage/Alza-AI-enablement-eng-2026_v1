import asyncio
import base64
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from alza_ai.gmail import (
    GmailMessageMetadata,
    GmailMessageRef,
    GmailTerminalError,
    HistoryPage,
    MessagePage,
    WatchState,
)
from alza_ai.main import create_app
from alza_ai.processing import PROCESSING_COLLECTION
from alza_ai.synchronization import (
    GmailPush,
    MailboxSynchronizer,
    PubSubWorkPublisher,
    SynchronizationStore,
    SyncResult,
    WorkMetadata,
    parse_gmail_push_envelope,
)

MAILBOX_KEY = "mailbox-opaque"
MAILBOX_ADDRESS = "assistant@example.test"
TOPIC = "projects/project/topics/gmail-notifications"
ACTIVATED_AT = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.now = ACTIVATED_AT

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
        self.client = client
        self.path = path

    def get(self, *, transaction: object | None = None) -> FakeSnapshot:
        del transaction
        return FakeSnapshot(self.client.documents.get(self.path))


class FakeCollection:
    def __init__(self, client: FakeFirestore, name: str) -> None:
        self.client = client
        self.name = name

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self.client, f"{self.name}/{document_id}")


class FakeTransaction:
    def __init__(self, client: FakeFirestore) -> None:
        self.client = client

    def set(self, document: FakeDocument, value: Mapping[str, object]) -> None:
        self.client.documents[document.path] = dict(value)

    def update(self, document: FakeDocument, value: Mapping[str, object]) -> None:
        self.client.documents[document.path].update(value)


class FakeFirestore:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}
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
            return operation(transaction)


class FakeGmail:
    def __init__(self) -> None:
        self.history_pages: dict[str | None, HistoryPage] = {
            None: HistoryPage((), "100", None)
        }
        self.unread_pages: dict[str | None, MessagePage] = {None: MessagePage((), None)}
        self.metadata: dict[str, GmailMessageMetadata] = {}
        self.watch_results = [WatchState("500", 2_000_000_000_000)]
        self.history_error: GmailTerminalError | None = None
        self.history_calls: list[tuple[str, str | None]] = []
        self.unread_calls: list[str | None] = []
        self.metadata_calls: list[str] = []
        self.watch_calls: list[str] = []

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> HistoryPage:
        self.history_calls.append((start_history_id, page_token))
        if self.history_error is not None:
            raise self.history_error
        return self.history_pages[page_token]

    def list_unread(self, page_token: str | None = None) -> MessagePage:
        self.unread_calls.append(page_token)
        return self.unread_pages[page_token]

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata:
        self.metadata_calls.append(message_id)
        return self.metadata[message_id]

    def start_watch(self, topic_name: str) -> WatchState:
        self.watch_calls.append(topic_name)
        if len(self.watch_results) > 1:
            return self.watch_results.pop(0)
        return self.watch_results[0]


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[WorkMetadata] = []
        self.calls = 0
        self.fail_on_call: int | None = None

    def publish(self, work: WorkMetadata) -> None:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("synthetic publication failure")
        self.published.append(work)


def make_store(
    *, clock: MutableClock | None = None
) -> tuple[SynchronizationStore, FakeFirestore, MutableClock]:
    selected_clock = clock or MutableClock()
    client = FakeFirestore()
    store = SynchronizationStore(
        client,
        clock=selected_clock,
        transaction_runner=client.run_transaction,
    )
    return store, client, selected_clock


def activate(store: SynchronizationStore, history_id: str = "100") -> None:
    assert store.activate_or_renew(
        MAILBOX_KEY, WatchState(history_id, 2_000_000_000_000)
    )


def make_synchronizer(
    *,
    store: SynchronizationStore,
    gmail: FakeGmail,
    publisher: FakePublisher,
) -> MailboxSynchronizer:
    return MailboxSynchronizer(
        mailbox_key=MAILBOX_KEY,
        mailbox_address=MAILBOX_ADDRESS,
        topic_name=TOPIC,
        store=store,
        gmail=gmail,
        publisher=publisher,
    )


def added_message(message_id: str, history_id: str = "101") -> Mapping[str, object]:
    return {
        "id": history_id,
        "messagesAdded": [
            {
                "message": {
                    "id": message_id,
                    "threadId": f"thread-{message_id}",
                    "labelIds": ["INBOX", "UNREAD"],
                }
            }
        ],
    }


def inbox_label_added(message_id: str, history_id: str = "102") -> Mapping[str, object]:
    return {
        "id": history_id,
        "labelsAdded": [
            {
                "message": {
                    "id": message_id,
                    "threadId": f"thread-{message_id}",
                    "labelIds": ["INBOX", "UNREAD"],
                },
                "labelIds": ["INBOX"],
            }
        ],
    }


def gmail_envelope(address: str, history_id: str) -> dict[str, object]:
    data = base64.b64encode(
        json.dumps({"emailAddress": address, "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data}}


def mailbox_document(client: FakeFirestore) -> dict[str, object]:
    matches = [
        value
        for path, value in client.documents.items()
        if path.startswith("mailbox-synchronization/")
    ]
    assert len(matches) == 1
    return matches[0]


async def post(
    app: FastAPI, path: str, payload: object | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post(path, json=payload)


def test_sync_01_duplicate_push_envelopes_publish_and_advance_once() -> None:
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    gmail.history_pages[None] = HistoryPage(
        (added_message("message-1"), added_message("message-1")), "101", None
    )
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)
    app = create_app(mailbox_synchronizer=synchronizer)
    payload = gmail_envelope(MAILBOX_ADDRESS.upper(), "101")

    first = asyncio.run(post(app, "/events/gmail", payload))
    duplicate = asyncio.run(post(app, "/events/gmail", payload))
    wrong_mailbox = asyncio.run(
        post(app, "/events/gmail", gmail_envelope("private@example.test", "102"))
    )
    malformed = asyncio.run(
        post(app, "/events/gmail", {"message": {"data": "not-base64"}})
    )

    assert [
        first.status_code,
        duplicate.status_code,
        wrong_mailbox.status_code,
        malformed.status_code,
    ] == [204, 204, 204, 204]
    assert all(
        response.content == b""
        for response in (first, duplicate, wrong_mailbox, malformed)
    )
    assert gmail.history_calls == [("100", None)]
    assert mailbox_document(client)["history_cursor"] == "101"
    assert len(publisher.published) == 1
    assert publisher.published[0].as_dict() == {
        "schema_version": 1,
        "mailbox_key": MAILBOX_KEY,
        "message_id": "message-1",
        "history_id": "101",
        "correlation_id": hashlib.sha256(
            f"{MAILBOX_KEY}:message-1:101".encode()
        ).hexdigest(),
    }
    assert "private@example.test" not in json.dumps(
        [work.as_dict() for work in publisher.published]
    )


def test_sync_02_empty_history_visibility_gap_reconciles_unread_before_ack() -> None:
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    gmail.history_pages[None] = HistoryPage((), "101", None)
    gmail.unread_pages[None] = MessagePage(
        (GmailMessageRef("delayed", "thread-delayed"),), None
    )
    gmail.metadata["delayed"] = GmailMessageMetadata(
        "delayed",
        "thread-delayed",
        int(ACTIVATED_AT.timestamp() * 1000),
        ("INBOX", "UNREAD"),
    )
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert synchronizer.handle_push(GmailPush(MAILBOX_ADDRESS, "101")) is SyncResult.ACK

    assert gmail.history_calls == [("100", None)]
    assert gmail.unread_calls == [None]
    assert [work.message_id for work in publisher.published] == ["delayed"]
    assert mailbox_document(client)["history_cursor"] == "101"


def test_sync_02_partial_publication_preserves_cursor_for_safe_replay() -> None:
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    gmail.history_pages[None] = HistoryPage(
        (added_message("message-1"), added_message("message-2")), "101", None
    )
    publisher = FakePublisher()
    publisher.fail_on_call = 2
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert (
        synchronizer.handle_push(GmailPush(MAILBOX_ADDRESS, "101")) is SyncResult.RETRY
    )
    failed_state = mailbox_document(client)
    assert failed_state["history_cursor"] == "100"
    assert failed_state["history_page_token"] is None
    assert failed_state["history_item_offset"] == 0

    publisher.fail_on_call = None
    assert synchronizer.handle_push(GmailPush(MAILBOX_ADDRESS, "101")) is SyncResult.ACK

    assert [work.message_id for work in publisher.published] == [
        "message-1",
        "message-1",
        "message-2",
    ]
    assert mailbox_document(client)["history_cursor"] == "101"


def test_sync_01_history_pages_publish_label_additions_and_deduplicate() -> None:
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    gmail.history_pages = {
        None: HistoryPage((added_message("message-1"),), "101", "page-2"),
        "page-2": HistoryPage(
            (
                inbox_label_added("message-1"),
                inbox_label_added("message-2"),
            ),
            "102",
            None,
        ),
    }
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert synchronizer.handle_push(GmailPush(MAILBOX_ADDRESS, "102")) is SyncResult.ACK

    assert gmail.history_calls == [("100", None), ("100", "page-2")]
    assert [work.message_id for work in publisher.published] == [
        "message-1",
        "message-2",
    ]
    assert mailbox_document(client)["history_cursor"] == "102"


def test_sync_01_concurrent_synchronization_has_one_gmail_owner() -> None:
    store, _, _ = make_store()
    activate(store)
    entered = threading.Event()
    release = threading.Event()

    class BlockingGmail(FakeGmail):
        def list_history(
            self, start_history_id: str, page_token: str | None = None
        ) -> HistoryPage:
            entered.set()
            assert release.wait(timeout=2)
            return super().list_history(start_history_id, page_token)

    gmail = BlockingGmail()
    gmail.history_pages[None] = HistoryPage((added_message("message-1"),), "101", None)
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)
    push = GmailPush(MAILBOX_ADDRESS, "101")

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(synchronizer.handle_push, push)
        assert entered.wait(timeout=2)
        overlap = pool.submit(synchronizer.handle_push, push)
        assert overlap.result(timeout=2) is SyncResult.ACK
        release.set()
        assert owner.result(timeout=2) is SyncResult.ACK

    assert gmail.history_calls == [("100", None)]
    assert [work.message_id for work in publisher.published] == ["message-1"]


def test_sync_02_stale_cursor_recovers_only_after_unread_publication() -> None:
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    gmail.history_error = GmailTerminalError("gmail_http_error", 404)
    gmail.unread_pages[None] = MessagePage(
        (GmailMessageRef("dropped", "thread-1"),), None
    )
    gmail.metadata["dropped"] = GmailMessageMetadata(
        "dropped",
        "thread-1",
        int(ACTIVATED_AT.timestamp() * 1000),
        ("INBOX", "UNREAD"),
    )
    publisher = FakePublisher()
    publisher.fail_on_call = 1
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert (
        synchronizer.handle_push(GmailPush(MAILBOX_ADDRESS, "999")) is SyncResult.RETRY
    )
    assert mailbox_document(client)["history_cursor"] == "100"
    assert gmail.watch_calls == []

    publisher.fail_on_call = None
    assert synchronizer.handle_push(GmailPush(MAILBOX_ADDRESS, "999")) is SyncResult.ACK
    state = mailbox_document(client)
    assert state["history_cursor"] == "500"
    assert state["watch_history_id"] == "500"
    assert gmail.watch_calls == [TOPIC]
    assert [work.message_id for work in publisher.published] == ["dropped"]


def processing_path(message_id: str) -> str:
    record_id = hashlib.sha256(f"{MAILBOX_KEY}:{message_id}".encode()).hexdigest()
    return f"{PROCESSING_COLLECTION}/{record_id}"


def test_sync_03_reconciliation_recovers_dropped_but_not_pre_activation_or_final() -> (
    None
):
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    message_ids = ("dropped", "pre-existing", "completed", "terminal", "retryable")
    gmail.unread_pages[None] = MessagePage(
        tuple(GmailMessageRef(value, f"thread-{value}") for value in message_ids),
        None,
    )
    for message_id in message_ids:
        received_at = ACTIVATED_AT
        if message_id == "pre-existing":
            received_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        gmail.metadata[message_id] = GmailMessageMetadata(
            message_id,
            f"thread-{message_id}",
            int(received_at.timestamp() * 1000),
            ("INBOX", "UNREAD"),
        )
    client.documents[processing_path("completed")] = {"state": "completed"}
    client.documents[processing_path("terminal")] = {"state": "terminal_error"}
    client.documents[processing_path("retryable")] = {"state": "processing"}
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert synchronizer.reconcile_unread() is SyncResult.ACK
    assert synchronizer.reconcile_unread() is SyncResult.ACK

    assert [work.message_id for work in publisher.published] == [
        "dropped",
        "retryable",
        "dropped",
        "retryable",
    ]
    assert set(gmail.metadata_calls) == {"dropped", "pre-existing", "retryable"}
    assert mailbox_document(client)["history_cursor"] == "100"


def test_sync_03_reconciliation_stops_at_500_and_resumes_from_offset() -> None:
    store, client, _ = make_store()
    activate(store)
    gmail = FakeGmail()
    references = tuple(
        GmailMessageRef(f"message-{index}", f"thread-{index}") for index in range(501)
    )
    gmail.unread_pages[None] = MessagePage(references, None)
    for reference in references:
        gmail.metadata[reference.message_id] = GmailMessageMetadata(
            reference.message_id,
            reference.thread_id,
            int(ACTIVATED_AT.timestamp() * 1000),
            ("INBOX", "UNREAD"),
        )
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert synchronizer.reconcile_unread() is SyncResult.RETRY
    checkpoint = mailbox_document(client)
    assert len(publisher.published) == 500
    assert checkpoint["reconciliation_page_token"] is None
    assert checkpoint["reconciliation_item_offset"] == 500

    assert synchronizer.reconcile_unread() is SyncResult.ACK
    assert len(publisher.published) == 501
    assert gmail.unread_calls == [None, None]
    assert mailbox_document(client)["reconciliation_item_offset"] == 0


def test_sync_03_watch_activation_is_immutable_and_daily_renewal_does_not_jump_cursor() -> (
    None
):
    store, client, _ = make_store()
    gmail = FakeGmail()
    gmail.watch_results = [
        WatchState("100", 2_000_000_000_000),
        WatchState("200", 2_000_100_000_000),
    ]
    publisher = FakePublisher()
    synchronizer = make_synchronizer(store=store, gmail=gmail, publisher=publisher)

    assert synchronizer.renew_watch() is SyncResult.ACK
    first = dict(mailbox_document(client))
    assert first["activated_at"] == ACTIVATED_AT
    assert first["history_cursor"] == "100"
    assert gmail.unread_calls == [None]

    assert synchronizer.renew_watch() is SyncResult.ACK
    renewed = mailbox_document(client)
    assert renewed["activated_at"] == ACTIVATED_AT
    assert renewed["history_cursor"] == "100"
    assert renewed["watch_history_id"] == "200"
    assert renewed["watch_expiration_ms"] == 2_000_100_000_000
    assert gmail.unread_calls == [None]


def test_sync_01_work_publisher_serializes_exact_metadata_only_schema() -> None:
    class Future:
        def __init__(self) -> None:
            self.waited = False

        def result(self) -> None:
            self.waited = True

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes]] = []
            self.future = Future()

        def publish(self, topic: str, data: bytes) -> Future:
            self.calls.append((topic, data))
            return self.future

    client = Client()
    publisher = PubSubWorkPublisher(client, "projects/project/topics/email-work")
    work = WorkMetadata.create(MAILBOX_KEY, "message-1", "101")

    publisher.publish(work)

    assert client.calls == [
        (
            "projects/project/topics/email-work",
            json.dumps(work.as_dict(), separators=(",", ":"), sort_keys=True).encode(),
        )
    ]
    assert client.future.waited


def test_api_01_scheduler_routes_map_ack_and_retry_to_empty_responses() -> None:
    class StubSynchronizer:
        def __init__(self) -> None:
            self.notifications: list[GmailPush] = []

        def handle_push(self, push: GmailPush) -> SyncResult:
            self.notifications.append(push)
            return SyncResult.ACK

        def renew_watch(self) -> SyncResult:
            return SyncResult.RETRY

        def reconcile_unread(self) -> SyncResult:
            return SyncResult.ACK

    stub = StubSynchronizer()
    app = create_app(mailbox_synchronizer=stub)

    event = asyncio.run(
        post(app, "/events/gmail", gmail_envelope(MAILBOX_ADDRESS, "101"))
    )
    renewal = asyncio.run(post(app, "/jobs/renew-watch"))
    reconciliation = asyncio.run(post(app, "/jobs/reconcile-unread"))

    assert [event.status_code, renewal.status_code, reconciliation.status_code] == [
        204,
        503,
        204,
    ]
    assert event.content == renewal.content == reconciliation.content == b""
    assert stub.notifications == [GmailPush(MAILBOX_ADDRESS, "101")]


def test_sync_01_push_parser_rejects_non_decimal_history_without_reflection() -> None:
    assert parse_gmail_push_envelope(
        gmail_envelope(MAILBOX_ADDRESS, "101")
    ) == GmailPush(MAILBOX_ADDRESS, "101")
    assert (
        parse_gmail_push_envelope(gmail_envelope(MAILBOX_ADDRESS, "not-history"))
        is None
    )
    assert (
        parse_gmail_push_envelope({"message": {"data": "Private raw marker"}}) is None
    )


def test_sync_01_push_parser_accepts_unpadded_base64url_from_gmail() -> None:
    notification = json.dumps(
        {"emailAddress": MAILBOX_ADDRESS, "historyId": "101"}
    ).encode()
    encoded = base64.urlsafe_b64encode(notification).decode().rstrip("=")

    assert parse_gmail_push_envelope({"message": {"data": encoded}}) == GmailPush(
        MAILBOX_ADDRESS, "101"
    )


def test_sync_01_push_parser_normalizes_numeric_gmail_history_id() -> None:
    notification = json.dumps(
        {"emailAddress": MAILBOX_ADDRESS, "historyId": 101}
    ).encode()
    encoded = base64.urlsafe_b64encode(notification).decode().rstrip("=")

    assert parse_gmail_push_envelope({"message": {"data": encoded}}) == GmailPush(
        MAILBOX_ADDRESS, "101"
    )
