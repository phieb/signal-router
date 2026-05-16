import asyncio
import errno
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import aiohttp.web
import websockets
from websockets.exceptions import ConnectionClosed

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("signal-router")

SIGNAL_CLI_URL = os.environ["SIGNAL_CLI_URL"].rstrip("/")
WEBHOOK_URLS = [u.strip() for u in os.environ["WEBHOOK_URLS"].split(",") if u.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
API_KEY = os.getenv("API_KEY", "")
SEND_PORT = 8081
SENDERS_FILE = Path(os.getenv("SENDERS_FILE", "/data/senders.json"))
ALLOWLIST_SENDERS = os.getenv("ALLOWLIST_SENDERS", "").strip().lower() in ("1", "true", "yes")


def _load_senders() -> set[str]:
    if SENDERS_FILE.exists():
        try:
            data = json.loads(SENDERS_FILE.read_text())
            if isinstance(data, list):
                return {s for s in data if isinstance(s, str) and s}
        except Exception as exc:
            log.warning("could not read %s: %s", SENDERS_FILE, exc)
    return set()


def _save_senders() -> None:
    SENDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SENDERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ALLOWED_SENDERS)))
    tmp.replace(SENDERS_FILE)


ALLOWED_SENDERS: set[str] = _load_senders()

SIGNAL_HTTP_URL = SIGNAL_CLI_URL.replace("ws://", "http://").replace("wss://", "https://")

BACKOFF_INITIAL = 1
BACKOFF_MAX = 60
BACKOFF_FACTOR = 2

shutdown = asyncio.Event()


# ── Startup: discover phone number from signal-cli ───────────────────────────

async def discover_phone_number() -> str:
    backoff = BACKOFF_INITIAL
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{SIGNAL_HTTP_URL}/v1/accounts",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                    if data:
                        if len(data) > 1:
                            log.warning("signal-cli has %d accounts, using first: %s", len(data), data[0])
                        else:
                            log.info("discovered phone number: %s", data[0])
                        return data[0]
                    log.warning("signal-cli returned empty accounts list, retrying in %ds", backoff)
        except aiohttp.ClientConnectorError as exc:
            if exc.os_error and exc.os_error.errno == errno.ECONNREFUSED:
                log.warning(
                    "signal-cli is reachable but not accepting connections — "
                    "account not registered yet? "
                    "Run: docker compose run --rm signal-cli link -n signal-router "
                    "— retrying in %ds", backoff
                )
            else:
                log.warning("could not reach signal-cli: %s — retrying in %ds", exc, backoff)
        except Exception as exc:
            log.warning("could not reach signal-cli: %s — retrying in %ds", exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)


# ── Receive: WebSocket → webhooks ─────────────────────────────────────────────

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
        if ALLOWLIST_SENDERS and sender not in ALLOWED_SENDERS:
            log.debug("ignored message from %s (not in allowlist)", sender)
            continue

        log.info("message from %s → forwarding to %d webhook(s)", sender, len(WEBHOOK_URLS))
        await forward(session, envelope)


async def run_receiver(phone_number: str) -> None:
    ws_url = f"{SIGNAL_CLI_URL}/v1/receive/{phone_number}"
    backoff = BACKOFF_INITIAL
    async with aiohttp.ClientSession() as session:
        while not shutdown.is_set():
            try:
                log.info("connecting to %s", ws_url)
                async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
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


# ── Send: HTTP POST /send → signal-cli ───────────────────────────────────────

async def handle_send(request: aiohttp.web.Request) -> aiohttp.web.Response:
    if API_KEY and request.headers.get("X-Api-Key") != API_KEY:
        return aiohttp.web.Response(status=401, text="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Invalid JSON")

    to = body.get("to")
    message = body.get("message")

    if not to or not message:
        return aiohttp.web.Response(status=400, text="Missing 'to' or 'message'")

    recipients = [to] if isinstance(to, str) else to

    async with aiohttp.ClientSession() as session:
        payload = {
            "message": message,
            "number": request.app["phone_number"],
            "recipients": recipients,
        }
        try:
            async with session.post(
                f"{SIGNAL_HTTP_URL}/v1/send",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.text()
                log.info("send to %s → signal-cli %s", recipients, resp.status)
                return aiohttp.web.Response(status=resp.status, text=body, content_type="application/json")
        except Exception as exc:
            log.error("send failed: %s", exc)
            return aiohttp.web.Response(status=502, text=str(exc))


async def handle_senders_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    if API_KEY and request.headers.get("X-Api-Key") != API_KEY:
        return aiohttp.web.Response(status=401, text="Unauthorized")
    return aiohttp.web.json_response(sorted(ALLOWED_SENDERS) if ALLOWED_SENDERS else [])


async def handle_senders_post(request: aiohttp.web.Request) -> aiohttp.web.Response:
    if API_KEY and request.headers.get("X-Api-Key") != API_KEY:
        return aiohttp.web.Response(status=401, text="Unauthorized")
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400, text="Invalid JSON")
    number = body.get("number", "").strip()
    if not number:
        return aiohttp.web.Response(status=400, text="Missing 'number'")
    ALLOWED_SENDERS.add(number)
    _save_senders()
    log.info("allowlist: added %s (total: %d)", number, len(ALLOWED_SENDERS))
    return aiohttp.web.json_response({"added": number, "senders": sorted(ALLOWED_SENDERS)})


async def handle_senders_delete(request: aiohttp.web.Request) -> aiohttp.web.Response:
    if API_KEY and request.headers.get("X-Api-Key") != API_KEY:
        return aiohttp.web.Response(status=401, text="Unauthorized")
    number = request.match_info["number"]
    if number not in ALLOWED_SENDERS:
        return aiohttp.web.Response(status=404, text="Number not in allowlist")
    ALLOWED_SENDERS.discard(number)
    _save_senders()
    log.info("allowlist: removed %s (total: %d)", number, len(ALLOWED_SENDERS))
    return aiohttp.web.json_response({"removed": number, "senders": sorted(ALLOWED_SENDERS)})


async def run_sender(phone_number: str) -> None:
    app = aiohttp.web.Application()
    app["phone_number"] = phone_number
    app.router.add_post("/send", handle_send)
    app.router.add_get("/senders", handle_senders_get)
    app.router.add_post("/senders", handle_senders_post)
    app.router.add_delete("/senders/{number}", handle_senders_delete)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", SEND_PORT)
    await site.start()
    log.info("send API listening on port %d", SEND_PORT)
    await shutdown.wait()
    await runner.cleanup()


# ── Entry point ───────────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    log.info("received signal %s, shutting down", signum)
    shutdown.set()


def main() -> None:
    if not WEBHOOK_URLS:
        log.error("WEBHOOK_URLS is not configured")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not ALLOWLIST_SENDERS:
        log.warning("ALLOWLIST_SENDERS is not set — all senders accepted")
    elif not ALLOWED_SENDERS:
        log.warning("ALLOWLIST_SENDERS is active but senders.json is empty — no messages will be forwarded")

    async def _run() -> None:
        phone_number = await discover_phone_number()
        log.info(
            "signal-router starting | phone=%s webhooks=%d allowlist=%s senders=%d send_port=%d",
            phone_number,
            len(WEBHOOK_URLS),
            "on" if ALLOWLIST_SENDERS else "off",
            len(ALLOWED_SENDERS),
            SEND_PORT,
        )
        await asyncio.gather(run_receiver(phone_number), run_sender(phone_number))

    asyncio.run(_run())
    log.info("shutdown complete")


if __name__ == "__main__":
    main()
