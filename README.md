# signal-router

Lightweight bridge between [signal-cli](https://github.com/AsamK/signal-cli) and webhook consumers like n8n. Listens on signal-cli's JSON-RPC WebSocket and forwards incoming Signal messages via HTTP POST.

```
Signal app → signal-cli (WebSocket) → signal-router → your webhook (n8n, etc.)
```

## Quick start (pre-built image)

Create a `docker-compose.yml` with the following content:

```yaml
services:
  signal-cli:
    image: ghcr.io/asamk/signal-cli:latest-native
    restart: unless-stopped
    # The port in `daemon --http` must match the port in SIGNAL_CLI_URL below.
    command: >
      -a +43123456789
      daemon --http 0.0.0.0:8080
    volumes:
      - signal-data:/var/lib/signal-cli
    networks:
      - signal-net

  signal-router:
    image: ghcr.io/phieb/signal-router:latest
    restart: unless-stopped
    environment:
      SIGNAL_CLI_URL: ws://signal-cli:8080  # must match signal-cli's --http port above
      SIGNAL_PHONE_NUMBER: "+43123456789"
      WEBHOOK_URLS: "http://n8n:5678/webhook/signal"
      WEBHOOK_SECRET: ""         # optional, sent as X-Webhook-Secret header
      ALLOWED_SENDERS: ""        # optional, comma-separated e.g. "+43111,+43222"
      API_KEY: ""                # optional, required as X-Api-Key header on /send
      LOG_LEVEL: INFO
    ports:
      - "8081:8081"
    depends_on:
      - signal-cli
    networks:
      - signal-net

volumes:
  signal-data:

networks:
  signal-net:
```

Then connect your number (see [Connecting a number](#connecting-a-number) below) and start:

```bash
docker compose up -d
```

## Development setup (build from source)

```bash
git clone https://github.com/phieb/signal-router.git
cd signal-router
cp .env.example .env  # edit to taste
```

See the [Makefile reference](#makefile-reference) for register/link/verify commands.

## Requirements

- Docker + Docker Compose
- A Signal account (phone number)

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```env
SIGNAL_PHONE_NUMBER=+43123456789
WEBHOOK_URLS=http://n8n:5678/webhook/signal
WEBHOOK_SECRET=               # optional, sent as X-Webhook-Secret header when forwarding
ALLOWED_SENDERS=              # optional, comma-separated whitelist e.g. +43111,+43222
API_KEY=                      # optional, required as X-Api-Key header on /send requests
LOG_LEVEL=INFO
```

> **Port note:** The port in signal-cli's `daemon --http 0.0.0.0:<port>` command and in `SIGNAL_CLI_URL` must always match — they refer to the same internal listener. The send API always listens on 8081 (a different port) and is not configurable.

## Connecting a number

There are two ways to connect a phone number. Choose the one that fits your situation:

### Option A — Link as secondary device (recommended)

Use this if the number already has a Signal account on your phone. signal-cli becomes a secondary device alongside your phone — **your existing account, contacts, and message history are preserved**.

```bash
# pre-built image:
docker compose run --rm signal-cli link -n "signal-router"

# from source:
make link
```

This prints a `sgnl://linkdevice?...` URL. Convert it to a QR code (e.g. `qrencode -t ansi '<url>'` or any online tool), then scan it in the Signal app on your phone under **Settings → Linked Devices → Link New Device**.

### Option B — Register a new account

Use this if the number has no existing Signal account, or you're using a dedicated SIM/VoIP number just for this service. **This displaces any existing Signal account on the number.**

```bash
# pre-built image:
docker compose run --rm signal-cli -a +43XXXXXXXXX register
docker compose run --rm signal-cli -a +43XXXXXXXXX verify 123456

# from source:
make register
make verify CODE=123456
```

Signal sends a verification code via SMS. After verifying, start the services normally.

## Sending messages

The router exposes a `POST /send` endpoint so other services (e.g. n8n) can send Signal messages without talking to signal-cli directly.

```bash
curl -X POST http://localhost:8081/send \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: your_api_key" \
  -d '{"to": "+43111222333", "message": "Hello from n8n!"}'
```

`to` can be a single number or a list of numbers. If `API_KEY` is not set, the endpoint is unauthenticated.

From another Docker container on the same network, use `http://signal-router:8081/send` as the URL.

## Managing the sender allowlist

Set `ALLOWLIST_SENDERS=true` to only forward messages from numbers in the allowlist. When not set (or `false`), all senders are accepted and a warning is logged on startup.

The allowlist is stored in `/data/senders.json` (persisted via the `router-data` volume). It can be edited manually before starting the container, or managed at runtime via the `/senders` API without restarting. The same `API_KEY` auth applies as for `/send`.

```bash
# list current allowlist
curl http://localhost:8081/senders -H "X-Api-Key: your_api_key"

# add a number
curl -X POST http://localhost:8081/senders \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: your_api_key" \
  -d '{"number": "+43111222333"}'

# remove a number (URL-encode the + as %2B)
curl -X DELETE http://localhost:8081/senders/%2B43111222333 \
  -H "X-Api-Key: your_api_key"
```

## Webhook payload

Every incoming message is forwarded as a JSON POST:

```json
{
  "source": "signal-router",
  "timestamp": "2024-01-15T10:30:00+00:00",
  "envelope": {
    "source": "+43111222333",
    "sourceDevice": 1,
    "dataMessage": {
      "message": "Hello!",
      "timestamp": 1705312200000
    }
  }
}
```

The `envelope` is the raw signal-cli payload — all fields are passed through as-is.

If `WEBHOOK_SECRET` is set, every request includes an `X-Webhook-Secret` header for verification on the receiving end.

## Multiple webhooks

`WEBHOOK_URLS` accepts a comma-separated list — all webhooks are called concurrently:

```env
WEBHOOK_URLS=http://n8n:5678/webhook/signal,http://other-service/hook
```

## Reconnection

The router reconnects automatically if signal-cli restarts, with exponential backoff (1s → 2s → 4s … → 60s max).

## Image notes

signal-cli uses the official image published by the signal-cli author:

| Tag | Runtime | Image size | Notes |
|---|---|---|---|
| `latest-native` | Native binary | ~50–100 MB | Fast startup, amd64 only |
| `latest` | JVM | ~300–400 MB | Works on all architectures incl. ARM |

`latest-native` is used by default. Switch to `latest` in `docker-compose.yml` if you're running on ARM (e.g. Raspberry Pi).

## Makefile reference

| Command | Description |
|---|---|
| `make link` | Link as secondary device (prints QR-scannable URL) |
| `make register` | Register a new account (request SMS code) |
| `make verify CODE=xxxxxx` | Complete registration |
| `make up` | Start both services |
| `make down` | Stop both services |
| `make logs` | Tail signal-router logs |
| `make build` | Rebuild the signal-router image |
