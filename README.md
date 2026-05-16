# signal-router

Lightweight bridge between [signal-cli](https://github.com/AsamK/signal-cli) and webhook consumers like n8n. Listens on signal-cli's JSON-RPC WebSocket and forwards incoming Signal messages via HTTP POST.

```
Signal app → signal-cli (WebSocket) → signal-router → your webhook (n8n, etc.)
```

## Quick start (pre-built image)

```bash
curl -O https://raw.githubusercontent.com/phieb/signal-router/main/docker-compose.example.yml
curl -O https://raw.githubusercontent.com/phieb/signal-router/main/.env.example
cp .env.example .env
# edit .env, then:
docker compose -f docker-compose.example.yml run --rm signal-cli -a +43XXXXXXXXX register
docker compose -f docker-compose.example.yml run --rm signal-cli -a +43XXXXXXXXX verify 123456
docker compose -f docker-compose.example.yml up -d
```

## Development setup (build from source)

Clone the repo and use the `Makefile`:

```bash
git clone https://github.com/phieb/signal-router.git
cd signal-router
cp .env.example .env  # edit to taste
make register
make verify CODE=123456
make up
```

## Requirements

- Docker + Docker Compose
- A phone number that can receive SMS (for registration)

## Configuration

```bash
cp .env.example .env
```

Edit `.env` — all options:

```env
SIGNAL_PHONE_NUMBER=+43123456789
WEBHOOK_URLS=http://n8n:5678/webhook/signal
WEBHOOK_SECRET=               # optional, sent as X-Webhook-Secret header
ALLOWED_SENDERS=              # optional, comma-separated whitelist e.g. +43111,+43222
LOG_LEVEL=INFO
```

### 2. Register your number (one-time)

```bash
make register        # Signal sends an SMS with a verification code
make verify CODE=123456
```

This runs signal-cli in a temporary container against the same persistent volume, so credentials are in place before the daemon starts.

### 3. Start

```bash
make up
make logs
```

You should see `connected` in the router logs within a few seconds.

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
| `make register` | Request SMS verification code |
| `make verify CODE=xxxxxx` | Complete registration |
| `make up` | Start both services |
| `make down` | Stop both services |
| `make logs` | Tail signal-router logs |
| `make build` | Rebuild the signal-router image |
