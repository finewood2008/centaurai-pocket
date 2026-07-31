.PHONY: bootstrap dev test smoke api mobile mobile-web mobile-verify desktop desktop-install

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
	cd apps/mobile && npm run start

mobile-web:
	cd apps/mobile && npm run web

mobile-verify:
	./scripts/build-mobile.sh

desktop:
	./scripts/build-desktop.sh

desktop-install: desktop
	./scripts/install-desktop-shortcut.sh
