import os
from collections.abc import Mapping
from typing import Protocol, cast

from fastapi import FastAPI, Request, Response

from alza_ai.processing import ProcessResult, WorkItem, parse_work_envelope
from alza_ai.synchronization import (
    GmailPush,
    SyncResult,
    parse_gmail_push_envelope,
)


class _ProcessingCoordinator(Protocol):
    async def process(self, work: WorkItem) -> ProcessResult: ...


class _MailboxSynchronizer(Protocol):
    def handle_push(self, push: GmailPush) -> SyncResult: ...

    def renew_watch(self) -> SyncResult: ...

    def reconcile_unread(self) -> SyncResult: ...


def create_app(
    *,
    processing_coordinator: object | None = None,
    mailbox_synchronizer: object | None = None,
) -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    coordinator = cast(_ProcessingCoordinator | None, processing_coordinator)
    synchronizer = cast(_MailboxSynchronizer | None, mailbox_synchronizer)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/events/gmail")
    async def gmail_event(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return Response(status_code=204)
        push = parse_gmail_push_envelope(payload)
        if push is None:
            return Response(status_code=204)
        if synchronizer is None:
            return Response(status_code=503)
        result = synchronizer.handle_push(push)
        return Response(status_code=204 if result is SyncResult.ACK else 503)

    @application.post("/jobs/process-message")
    async def process_message(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return Response(status_code=204)
        work = parse_work_envelope(payload)
        if work is None:
            return Response(status_code=204)
        if coordinator is None:
            return Response(status_code=503)
        result = await coordinator.process(work)
        return Response(status_code=204 if result is ProcessResult.ACK else 503)

    @application.post("/jobs/renew-watch")
    async def renew_watch() -> Response:
        if synchronizer is None:
            return Response(status_code=503)
        result = synchronizer.renew_watch()
        return Response(status_code=204 if result is SyncResult.ACK else 503)

    @application.post("/jobs/reconcile-unread")
    async def reconcile_unread() -> Response:
        if synchronizer is None:
            return Response(status_code=503)
        result = synchronizer.reconcile_unread()
        return Response(status_code=204 if result is SyncResult.ACK else 503)

    return application


def select_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    settings = os.environ if environ is None else environ
    mode = settings.get("ALZA_ENV", "local")
    if mode == "local":
        return create_app()
    if mode == "production":
        from alza_ai.runtime import create_production_app

        return create_production_app(settings)
    raise RuntimeError("app_mode_invalid")


app = select_app()
