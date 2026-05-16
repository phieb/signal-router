# signal-router

Lightweight bridge between [bbernhard/signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) and webhook consumers like n8n. Listens on signal-cli's WebSocket and forwards incoming Signal messages via HTTP POST.

```
Signal app → signal-cli (WebSocket) → signal-router → your webhook (n8n, etc.)
                  ↑
        consumers send via signal-cli's REST API directly
```

## Quick start (pre-built image)

Create a `docker-compose.yml` with the following content:

```yaml
services:
  signal-cli:
    image: bbernhard/signal-cli-rest-api:latest
    restart: unless-stopped
    environment:
      MODE: json-rpc
    ports:
      - "8080:8080"  # expose for register/link via curl; consumers reach it on the docker network
    volumes:
      - signal-data:/home/.local/share/signal-cli
    networks:
      - signal-net

  signal-router:
    image: ghcr.io/phieb/signal-router:latest
    restart: unless-stopped
    environment:
      SIGNAL_CLI_URL: http://signal-cli:8080
      WEBHOOK_URLS: "http://n8n:5678/webhook/signal"
      WEBHOOK_SECRET: ""         # optional, sent as X-Webhook-Secret header
      ALLOWLIST_SENDERS: "false" # set to "true" to only forward messages from numbers in /senders
      API_KEY: ""                # optional, required as X-Api-Key header on /senders
      LOG_LEVEL: INFO
    ports:
      - "8081:8081"
    volumes:
      - router-data:/data
    depends_on:
      - signal-cli
    networks:
      - signal-net

volumes:
  signal-data:
  router-data:

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
WEBHOOK_URLS=http://n8n:5678/webhook/signal
WEBHOOK_SECRET=               # optional, sent as X-Webhook-Secret header when forwarding
ALLOWLIST_SENDERS=false       # set to true to only forward messages from numbers in /senders
API_KEY=                      # optional, required as X-Api-Key header on /senders requests
LOG_LEVEL=INFO
```

The router auto-discovers the registered phone number from signal-cli. If you have multiple accounts, set `SIGNAL_PHONE_NUMBER` to disambiguate.

## Connecting a number

Bring the stack up first so signal-cli's REST API is reachable on `localhost:8080`:

```bash
docker compose up -d
```

There are two ways to connect a phone number. Choose the one that fits your situation.

### Option A — Link as secondary device (recommended)

Use this if the number already has a Signal account on your phone. signal-cli becomes a secondary device alongside your phone — **your existing account, contacts, and message history are preserved**.

```bash
make link
# saves link-qr.png — open it and scan in Signal → Settings → Linked Devices → Link New Device
```

Or call the API directly:

```bash
curl -o link-qr.png 'http://localhost:8080/v1/qrcodelink?device_name=signal-router'
```

### Option B — Register a new account

Use this if the number has no existing Signal account, or you're using a dedicated SIM/VoIP number just for this service. **This displaces any existing Signal account on the number.**

```bash
make register                 # requests SMS code
make verify CODE=123456       # completes registration
```

Or call the API directly:

```bash
curl -X POST -H 'Content-Type: application/json' -d '{"use_voice":false}' \
  http://localhost:8080/v1/register/+43XXXXXXXXX
curl -X POST -H 'Content-Type: application/json' -d '{}' \
  http://localhost:8080/v1/register/+43XXXXXXXXX/verify/123456
```

After verifying, the router will pick up the account on its next reconnect.

## Sending messages

The router itself does not expose a send endpoint — consumers talk to signal-cli's REST API directly on port 8080. See [bbernhard/signal-cli-rest-api docs](https://github.com/bbernhard/signal-cli-rest-api) for the full surface (send with attachments, group management, receipts, etc.).

Quick examples from another container on the same network:

```bash
# send text
curl -X POST -H 'Content-Type: application/json' http://signal-cli:8080/v2/send \
  -d '{"number": "+43YOURNUMBER", "recipients": ["+43111222333"], "message": "Hello"}'

# mark read
curl -X POST -H 'Content-Type: application/json' http://signal-cli:8080/v1/receipts/+43YOURNUMBER \
  -d '{"receipt_type": "read", "recipient": "+43111222333", "timestamp": 1705312200000}'
```

## Managing the sender allowlist

Set `ALLOWLIST_SENDERS=true` to only forward messages from numbers in the allowlist. When not set (or `false`), all senders are accepted and a warning is logged on startup.

The allowlist is stored in `/data/senders.json` (persisted via the `router-data` volume). It can be edited manually before starting the container, or managed at runtime via the `/senders` API without restarting. If `API_KEY` is set, the endpoint requires an `X-Api-Key` header.

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

## Makefile reference

| Command | Description |
|---|---|
| `make link` | Link as secondary device (writes `link-qr.png` for scanning) |
| `make register` | Request SMS code (for new/dedicated numbers) |
| `make verify CODE=xxxxxx` | Complete registration |
| `make up` | Start both services |
| `make down` | Stop both services |
| `make logs` | Tail signal-router logs |
| `make build` | Rebuild the signal-router image |
