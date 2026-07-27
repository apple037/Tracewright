.PHONY: demo stop logs reset restart test

## Start everything and print the console URL.
demo:
	@./run.sh

## Stop the demo.
stop:
	@./run.sh stop

## Follow the API and worker logs.
logs:
	@./run.sh logs

## Stop and delete the database, so the next start is clean.
reset:
	@./run.sh reset

## Pick up edits to config/*.yaml without a full rebuild.
restart:
	@docker compose restart app worker

## Run the test suites that need no database or model server. Same set as CI's
## fast job — keep the two in step, or a local pass stops meaning anything.
test:
	@uv run pytest tests/unit tests/contract tests/browser tests/e2e
