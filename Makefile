.PHONY: build up down link register verify logs

include .env
export

build:
	docker compose build signal-router

up:
	docker compose up -d

down:
	docker compose down

# Link as secondary device — saves QR code to link-qr.png; scan it in Signal → Linked Devices.
# Requires `make up` first so signal-cli is reachable on localhost:8080.
link:
	@curl -sSf -o link-qr.png "http://localhost:8080/v1/qrcodelink?device_name=signal-router" \
	  && echo "Saved QR code to link-qr.png — scan it in Signal → Settings → Linked Devices → Link New Device"

# Step 1 — request SMS verification code (for new/dedicated numbers)
register:
	curl -sSf -X POST -H "Content-Type: application/json" -d '{"use_voice":false}' \
	  "http://localhost:8080/v1/register/$(SIGNAL_PHONE_NUMBER)"

# Step 2 — make verify CODE=123456
verify:
	@test -n "$(CODE)" || (echo "usage: make verify CODE=123456" && exit 1)
	curl -sSf -X POST -H "Content-Type: application/json" -d '{}' \
	  "http://localhost:8080/v1/register/$(SIGNAL_PHONE_NUMBER)/verify/$(CODE)"

logs:
	docker compose logs -f signal-router
