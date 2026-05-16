import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("signal-router")

SIGNAL_CLI_URL = os.environ["SIGNAL_CLI_URL"].rstrip("/")
SIGNAL_PHONE_NUMBER = os.environ["SIGNAL_PHONE_NUMBER"]
WEBHOOK_URLS = [u.strip() for u in os.environ["WEBHOOK_URLS"].split(",") if u.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ALLOWED_SENDERS = {s.strip() for s in os.getenv("ALLOWED_SENDERS", "").split(",") if s.strip()}

WS_URL = f"{SIGNAL_CLI_URL}/v1/receive/{SIGNAL_PHONE_NUMBER}"

BACKOFF_INITIAL = 1
BACKOFF_MAX = 60
BACKOFF_FACTOR = 2

shutdown = asyncio.Event()


async def post_webhook(session: aiohttp.ClientSession, url: str, payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            log.info("webhook %s → %s", url, resp.status)
    except Exception as exc:
        log.error("webhook %s failed: %s", url, exc)


async def forward(session: aiohttp.ClientSession, envelope: dict) -> None:
    payload = {
        "source": "signal-router",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "envelope": envelope,
    }
    await asyncio.gather(*(post_webhook(session, url, payload) for url in WEBHOOK_URLS))


async def handle_messages(ws, session: aiohttp.ClientSession) -> None:
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("non-JSON message: %s", raw[:200])
            continue

        envelope = msg.get("envelope", {})

        if "dataMessage" not in envelope:
            continue

        sender = envelope.get("source", "")
        if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
            log.debug("ignored message from %s (not in allowlist)", sender)
            continue

        log.info("message from %s → forwarding to %d webhook(s)", sender, len(WEBHOOK_URLS))
        await forward(session, envelope)


async def run() -> None:
    backoff = BACKOFF_INITIAL
    async with aiohttp.ClientSession() as session:
        while not shutdown.is_set():
            try:
                log.info("connecting to %s", WS_URL)
                async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                    log.info("connected")
                    backoff = BACKOFF_INITIAL
                    await handle_messages(ws, session)
            except ConnectionClosed as exc:
                log.warning("connection closed: %s", exc)
            except OSError as exc:
                log.error("connection error: %s", exc)
            except Exception as exc:
                log.exception("unexpected error: %s", exc)

            if shutdown.is_set():
                break

            log.info("reconnecting in %ds", backoff)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)


def _handle_signal(signum, frame):
    log.info("received signal %s, shutting down", signum)
    shutdown.set()


def main() -> None:
    if not WEBHOOK_URLS:
        log.error("WEBHOOK_URLS is not configured")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "signal-router starting | phone=%s webhooks=%d allowlist=%s",
        SIGNAL_PHONE_NUMBER,
        len(WEBHOOK_URLS),
        ",".join(ALLOWED_SENDERS) if ALLOWED_SENDERS else "all",
    )

    asyncio.run(run())
    log.info("shutdown complete")


if __name__ == "__main__":
    main()
