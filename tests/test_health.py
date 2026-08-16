import asyncio

import httpx

from alza_ai.main import app


async def get_health() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get("/healthz")


def test_api_02_healthz_returns_stable_liveness_response() -> None:
    response = asyncio.run(get_health())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == b'{"status":"ok"}'
