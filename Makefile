.PHONY: sandbox test test-docker lint eval

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
