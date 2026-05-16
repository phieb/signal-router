.PHONY: build up down link register verify logs

include .env
export

build:
	docker compose build signal-router

up:
	docker compose up -d

down:
	docker compose down

# Link as secondary device (scan the printed URL as a QR code in Signal → Settings → Linked Devices)
link:
	docker compose run --rm signal-cli link -n "signal-router"

# Step 1 — request SMS verification code (for new/dedicated numbers)
register:
	docker compose run --rm signal-cli -a $(SIGNAL_PHONE_NUMBER) register

# Step 2 — make verify CODE=123456
verify:
	@test -n "$(CODE)" || (echo "usage: make verify CODE=123456" && exit 1)
	docker compose run --rm signal-cli -a $(SIGNAL_PHONE_NUMBER) verify $(CODE)

logs:
	docker compose logs -f signal-router
