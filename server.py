"""Render Web Service entrypoint.

Render's Web Service type requires a process listening on $PORT, but a LiveKit
agent is a worker: it dials out to LiveKit and never accepts inbound HTTP. So
this module runs both in one process and one event loop:

  * an aiohttp app on $PORT  - health check + the access-token endpoint the
    browser needs (the API secret must never reach the frontend)
  * the LiveKit AgentServer  - the worker from agent.py, with its own internal
    health port on WORKER_HEALTH_PORT (loopback only)

Local development is unchanged: `python agent.py console` / `dev` still work.
"""

import asyncio
import contextlib
import logging
import os
import signal
import uuid
from datetime import timedelta

from aiohttp import web
from dotenv import load_dotenv

from livekit import api
from livekit.agents import WorkerOptions
from livekit.agents.worker import AgentServer

from agent import entrypoint

load_dotenv()

logger = logging.getLogger("nova.server")

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

PORT = int(os.getenv("PORT", "8080"))
WORKER_HEALTH_PORT = int(os.getenv("WORKER_HEALTH_PORT", "8081"))

# Comma-separated frontend origins, e.g. "https://parklinedental.com.au".
# "*" is convenient for a first deploy but leaves the token endpoint open.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Optional shared secret. When set, the frontend must send X-API-Key; without
# it, anyone who finds this URL can open rooms and spend your inference credits.
TOKEN_API_KEY = os.getenv("TOKEN_API_KEY")

TOKEN_TTL = timedelta(minutes=int(os.getenv("TOKEN_TTL_MINUTES", "15")))


def _cors_headers(request: web.Request) -> dict[str, str]:
    origin = request.headers.get("Origin", "")
    if "*" in ALLOWED_ORIGINS:
        allow = "*"
    elif origin in ALLOWED_ORIGINS:
        allow = origin
    else:
        return {}
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
        "Access-Control-Max-Age": "86400",
    }


async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def token_preflight(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_cors_headers(request))


async def create_token(request: web.Request) -> web.Response:
    headers = _cors_headers(request)

    if TOKEN_API_KEY and request.headers.get("X-API-Key") != TOKEN_API_KEY:
        return web.json_response({"error": "unauthorized"}, status=401, headers=headers)

    try:
        body = await request.json()
    except Exception:
        body = {}

    # One fresh room per caller by default: two callers must never be dropped
    # into the same room, and Nova deletes the room when the call ends.
    room = str(body.get("room") or f"nova-{uuid.uuid4().hex[:12]}")
    identity = str(body.get("identity") or f"caller-{uuid.uuid4().hex[:8]}")
    name = str(body.get("name") or "Caller")

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,       # the caller's microphone
                can_subscribe=True,     # Nova's voice
                can_publish_data=True,
            )
        )
        .to_jwt()
    )

    return web.json_response(
        {
            "token": token,
            "serverUrl": LIVEKIT_URL,
            "room": room,
            "identity": identity,
        },
        headers=headers,
    )


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/", health),
            web.get("/healthz", health),
            web.options("/api/token", token_preflight),
            web.post("/api/token", create_token),
        ]
    )
    return app


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("token API listening on 0.0.0.0:%d", PORT)

    server = AgentServer.from_server_options(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # The worker's own health endpoint stays on loopback so it cannot
            # collide with the public listener above.
            host="127.0.0.1",
            port=WORKER_HEALTH_PORT,
        )
    )

    worker_task = asyncio.create_task(server.run())

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

    try:
        await asyncio.wait({worker_task, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Let calls in progress finish before the container goes away.
        if not worker_task.done():
            with contextlib.suppress(Exception):
                await server.drain()
            with contextlib.suppress(Exception):
                await server.aclose()
            await asyncio.gather(worker_task, return_exceptions=True)
        await runner.cleanup()

    if worker_task.done() and worker_task.exception():
        raise worker_task.exception()


if __name__ == "__main__":
    asyncio.run(main())
