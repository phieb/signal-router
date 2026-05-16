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


def _parse_endpoint(url: str) -> tuple[str, int]:
    url = url.strip().rstrip("/")
    if "://" in url:
        scheme, _, rest = url.partition("://")
        if scheme != "tcp":
            raise ValueError(
                f"SIGNAL_CLI_URL scheme {scheme!r} not supported — "
                "use tcp://host:port (signal-cli must run with `daemon --tcp`)"
            )
        url = rest
    host, _, port = url.partition(":")
    if not host or not port:
        raise ValueError(f"SIGNAL_CLI_URL must be host:port, got {url!r}")
    return host, int(port)


SIGNAL_CLI_HOST, SIGNAL_CLI_PORT = _parse_endpoint(os.environ["SIGNAL_CLI_URL"])
SIGNAL_PHONE_NUMBER = os.getenv("SIGNAL_PHONE_NUMBER", "").strip() or None
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

BACKOFF_INITIAL = 1
BACKOFF_MAX = 60
BACKOFF_FACTOR = 2

shutdown = asyncio.Event()
notifications: asyncio.Queue = asyncio.Queue()


class SignalCliClient:
    """Persistent TCP JSON-RPC client for signal-cli daemon."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.connected = asyncio.Event()

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self.connected.set()
        log.info("connected to signal-cli at %s:%d", self.host, self.port)

    async def close(self) -> None:
        self.connected.clear()
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("signal-cli disconnected"))
        self._pending.clear()

    async def read_loop(self) -> None:
        assert self.reader is not None
        while True:
            line = await self.reader.readline()
            if not line:
                raise ConnectionError("signal-cli closed connection")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-JSON line from signal-cli: %s", line[:200])
                continue
            if "id" in msg and msg["id"] in self._pending:
                fut = self._pending.pop(msg["id"])
                if not fut.done():
                    fut.set_result(msg)
            else:
                await notifications.put(msg)

    async def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if not self.writer or not self.connected.is_set():
            raise ConnectionError("not connected to signal-cli")
        self._next_id += 1
        req_id = self._next_id
        request: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params is not None:
            request["params"] = params
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            self.writer.write((json.dumps(request) + "\n").encode())
            await self.writer.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)


client: SignalCliClient | None = None


# ── Receive: JSON-RPC notifications → webhooks ────────────────────────────────

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


async def receiver_task() -> None:
    async with aiohttp.ClientSession() as session:
        while not shutdown.is_set():
            msg = await notifications.get()
            if msg.get("method") != "receive":
                continue
            envelope = msg.get("params", {}).get("envelope", {})
            if "dataMessage" not in envelope:
                continue
            sender = envelope.get("source") or envelope.get("sourceNumber", "")
            if ALLOWLIST_SENDERS and sender not in ALLOWED_SENDERS:
                log.debug("ignored message from %s (not in allowlist)", sender)
                continue
            log.info("message from %s → forwarding to %d webhook(s)", sender, len(WEBHOOK_URLS))
            await forward(session, envelope)


# ── Send API: HTTP POST /send → JSON-RPC ──────────────────────────────────────

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
    if client is None or not client.connected.is_set():
        return aiohttp.web.Response(status=503, text="signal-cli not connected")
    params: dict = {"recipient": recipients, "message": message}
    if SIGNAL_PHONE_NUMBER:
        params["account"] = SIGNAL_PHONE_NUMBER
    try:
        result = await client.call("send", params)
        if "error" in result:
            log.error("signal-cli send error: %s", result["error"])
            return aiohttp.web.json_response({"error": result["error"]}, status=502)
        log.info("send to %s OK", recipients)
        return aiohttp.web.json_response(result.get("result", {}))
    except asyncio.TimeoutError:
        log.error("send timed out")
        return aiohttp.web.Response(status=504, text="signal-cli timeout")
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


async def run_send_api() -> None:
    app = aiohttp.web.Application()
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


# ── Client lifecycle with reconnect ───────────────────────────────────────────

async def run_client() -> None:
    global client
    backoff = BACKOFF_INITIAL
    while not shutdown.is_set():
        client = SignalCliClient(SIGNAL_CLI_HOST, SIGNAL_CLI_PORT)
        try:
            await client.connect()
            backoff = BACKOFF_INITIAL
            await client.read_loop()
        except (ConnectionError, OSError) as exc:
            log.warning("signal-cli connection: %s — retrying in %ds", exc, backoff)
        except Exception as exc:
            log.exception("unexpected error in client: %s", exc)
        finally:
            await client.close()
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
        "signal-router starting | signal-cli=%s:%d phone=%s webhooks=%d allowlist=%s senders=%d send_port=%d",
        SIGNAL_CLI_HOST, SIGNAL_CLI_PORT, SIGNAL_PHONE_NUMBER or "(auto)",
        len(WEBHOOK_URLS), "on" if ALLOWLIST_SENDERS else "off",
        len(ALLOWED_SENDERS), SEND_PORT,
    )

    async def _run() -> None:
        await asyncio.gather(run_client(), receiver_task(), run_send_api())

    asyncio.run(_run())
    log.info("shutdown complete")


if __name__ == "__main__":
    main()
