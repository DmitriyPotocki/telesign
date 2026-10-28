"""HTTP health stub for Render: binds a port, answers /, /health, /healthz."""
import httpx

from bot.config import Config
from bot.webstub import start_stub, stop_stub


async def test_health_stub_answers_ok():
    runner, port = await start_stub(0)  # ephemeral port
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for path in ("/", "/health", "/healthz"):
                resp = await client.get(f"http://127.0.0.1:{port}{path}")
                assert resp.status_code == 200, path
                body = resp.json()
                assert body["status"] == "ok"
                assert body["service"] == "telesign-bot"
                assert body["uptime_seconds"] >= 0
    finally:
        await stop_stub(runner)


async def test_stub_stops_cleanly():
    runner, port = await start_stub(0)
    await stop_stub(runner)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.get(f"http://127.0.0.1:{port}/health")
        raise AssertionError("server must be down after stop")
    except httpx.TransportError:
        pass  # connection refused — expected


def test_port_reads_render_env(monkeypatch):
    monkeypatch.setenv("PORT", "10000")
    cfg = Config(_env_file=None)
    assert cfg.port == 10000  # Render assigns $PORT


def test_port_local_default(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)  # some CI shells export PORT
    cfg = Config(_env_file=None)
    assert cfg.port == 8080
