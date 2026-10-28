"""Minimal HTTP stub for PaaS hosts (Render).

A long-polling bot has no HTTP listener, but Render web services must bind
$PORT and answer a health check — otherwise the instance is considered dead.
This stub serves /, /health and /healthz with a tiny JSON payload while the
bot keeps polling Telegram in the same process. It also gives external
pingers (uptime monitors) something to hit to keep a free plan awake.
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

logger = logging.getLogger(__name__)

_STARTED_AT = time.time()


def _handler(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "telesign-bot",
            "uptime_seconds": int(time.time() - _STARTED_AT),
        }
    )


async def start_stub(port: int, host: str = "0.0.0.0") -> tuple[web.AppRunner, int]:
    """Bind the health stub; port=0 asks the OS for a free port (tests)."""
    app = web.Application()
    app.router.add_get("/", _handler)
    app.router.add_get("/health", _handler)
    app.router.add_get("/healthz", _handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    actual = port
    if port == 0 and runner.addresses:
        actual = runner.addresses[0][1]
    logger.info("health stub listening on %s:%s", host, actual)
    return runner, actual


async def stop_stub(runner: web.AppRunner) -> None:
    await runner.cleanup()
