.PHONY: sandbox test test-docker lint eval desktop desktop-build desktop-test

sandbox:            ## build the Docker image agent-written tools run in
	docker build -t sea-sandbox src/sea/sandbox

test:               ## unit tests, no network, no docker
	uv run pytest -q

test-docker:        ## tests that need the sandbox image
	uv run pytest -q -m docker

lint:
	uv run ruff check src tests && uv run ruff format --check src tests

eval:
	uv run python -m evals.run_evals

desktop:            ## open the desktop app (Tauri dev mode; starts the backend itself)
	cd desktop && pnpm install --silent && pnpm tauri dev

desktop-test:       ## type-check, unit tests and bundle for the desktop UI
	cd desktop && pnpm install --silent && pnpm test && pnpm build

desktop-build:      ## build a distributable app bundle into desktop/src-tauri/target/release/bundle
	cd desktop && pnpm install --silent && pnpm tauri build
