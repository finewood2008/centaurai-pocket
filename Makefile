.PHONY: bootstrap dev test smoke api mobile

bootstrap:
	./scripts/bootstrap.sh

dev:
	./scripts/dev.sh

test:
	./scripts/verify.sh

smoke:
	./scripts/smoke.sh

api:
	cd services/api && uv run centaur-pocket-api

mobile:
	cd apps/mobile && npm run web
