from typing import Protocol, cast

from fastapi import FastAPI, Request, Response

from alza_ai.processing import ProcessResult, WorkItem, parse_work_envelope


class _ProcessingCoordinator(Protocol):
    async def process(self, work: WorkItem) -> ProcessResult: ...


def create_app(*, processing_coordinator: object | None = None) -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    coordinator = cast(_ProcessingCoordinator | None, processing_coordinator)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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

    return application


app = create_app()
