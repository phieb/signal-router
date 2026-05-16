import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import aiohttp.web

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("signal-router")


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url


SIGNAL_CLI_URL = _normalize_url(os.environ["SIGNAL_CLI_URL"])
SIGNAL_PHONE_NUMBER = os.getenv("SIGNAL_PHONE_NUMBER", "").strip() or None
WEBHOOK_URLS = [u.strip() for u in os.environ["WEBHOOK_URLS"].split(",") if u.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
API_KEY = os.getenv("API_KEY", "")
ROUTER_PORT = 8081
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

BACKOFF_INITIAL = 1
BACKOFF_MAX = 60
BACKOFF_FACTOR = 2

shutdown = asyncio.Event()
notifications: asyncio.Queue = asyncio.Queue()


# ── Phone number discovery ────────────────────────────────────────────────────

async def discover_phone_number(session: aiohttp.ClientSession) -> str | None:
    try:
        async with session.get(f"{SIGNAL_CLI_URL}/v1/accounts") as resp:
            resp.raise_for_status()
            accounts = await resp.json()
    except aiohttp.ClientConnectorError as exc:
        log.warning("signal-cli not reachable at %s (%s)", SIGNAL_CLI_URL, exc)
        return None
    except Exception as exc:
        log.warning("could not list accounts: %s", exc)
        return None
    if not accounts:
        log.error("signal-cli has no registered account — run `make link` or `make register` first")
        return None
    if len(accounts) > 1:
        log.warning(
            "signal-cli has multiple accounts %s — using %s; set SIGNAL_PHONE_NUMBER to override",
            accounts, accounts[0],
        )
    return accounts[0]


# ── Receive: WebSocket → notifications queue ──────────────────────────────────

async def receive_ws(session: aiohttp.ClientSession, phone: str) -> None:
    ws_url = SIGNAL_CLI_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_url}/v1/receive/{phone}"
    log.info("connecting to %s", url)
    async with session.ws_connect(url, heartbeat=30) as ws:
        log.info("connected to signal-cli WebSocket")
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    log.warning("non-JSON from signal-cli: %s", msg.data[:200])
                    continue
                await notifications.put(data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
    raise ConnectionError("signal-cli WebSocket closed")


# ── Webhook fan-out ───────────────────────────────────────────────────────────

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


async def dispatcher() -> None:
    async with aiohttp.ClientSession() as session:
        while not shutdown.is_set():
            data = await notifications.get()
            envelope = data.get("envelope") or data
            if "dataMessage" not in envelope:
                continue
            sender = envelope.get("source") or envelope.get("sourceNumber", "")
            if ALLOWLIST_SENDERS and sender not in ALLOWED_SENDERS:
                log.debug("ignored message from %s (not in allowlist)", sender)
                continue
            log.info("message from %s → forwarding to %d webhook(s)", sender, len(WEBHOOK_URLS))
            await forward(session, envelope)


# ── Allowlist API ─────────────────────────────────────────────────────────────

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


async def run_router_api() -> None:
    app = aiohttp.web.Application()
    app.router.add_get("/senders", handle_senders_get)
    app.router.add_post("/senders", handle_senders_post)
    app.router.add_delete("/senders/{number}", handle_senders_delete)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", ROUTER_PORT)
    await site.start()
    log.info("router API listening on port %d", ROUTER_PORT)
    await shutdown.wait()
    await runner.cleanup()


# ── Receive loop with reconnect ───────────────────────────────────────────────

async def run_receiver() -> None:
    backoff = BACKOFF_INITIAL
    async with aiohttp.ClientSession() as session:
        phone = SIGNAL_PHONE_NUMBER
        while phone is None and not shutdown.is_set():
            phone = await discover_phone_number(session)
            if phone is None:
                log.info("retrying account discovery in %ds", backoff)
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
        if phone is None:
            return
        log.info("using phone number: %s", phone)
        backoff = BACKOFF_INITIAL
        while not shutdown.is_set():
            try:
                await receive_ws(session, phone)
            except (aiohttp.ClientError, ConnectionError, OSError) as exc:
                log.warning("receive connection: %s — retrying in %ds", exc, backoff)
            except Exception:
                log.exception("unexpected error in receiver")
            if shutdown.is_set():
                break
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)


def _handle_signal(signum: int, frame) -> None:
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

    log.info(
        "signal-router starting | signal-cli=%s phone=%s webhooks=%d allowlist=%s senders=%d router_port=%d",
        SIGNAL_CLI_URL, SIGNAL_PHONE_NUMBER or "(auto)",
        len(WEBHOOK_URLS), "on" if ALLOWLIST_SENDERS else "off",
        len(ALLOWED_SENDERS), ROUTER_PORT,
    )

    async def _run() -> None:
        await asyncio.gather(run_receiver(), dispatcher(), run_router_api())

    asyncio.run(_run())
    log.info("shutdown complete")


if __name__ == "__main__":
    main()
