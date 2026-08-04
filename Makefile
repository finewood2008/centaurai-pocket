.PHONY: bootstrap dev test smoke api mobile mobile-web mobile-verify desktop desktop-install observer-check observer-native-install observer-native-uninstall

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

observer-check:
	cd apps/wechat-observer-extension && npm run check && npm test
	python3 -m unittest discover -s tools/native-host/tests -v

observer-native-install:
	./tools/native-host/install-native-host.sh

observer-native-uninstall:
	./tools/native-host/uninstall-native-host.sh
