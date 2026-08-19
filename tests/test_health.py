import asyncio

import httpx
from fastapi.routing import APIRoute

from alza_ai.main import app


async def get_path(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get(path)


def test_api_02_health_returns_stable_five_route_contract() -> None:
    response = asyncio.run(get_path("/health"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == b'{"status":"ok"}'
    assert asyncio.run(get_path("/healthz")).status_code == 404
    assert {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    } == {
        ("GET", "/health"),
        ("POST", "/events/gmail"),
        ("POST", "/jobs/process-message"),
        ("POST", "/jobs/renew-watch"),
        ("POST", "/jobs/reconcile-unread"),
    }
