.PHONY: test test-hermes lint fmt doctor

PY ?= python3

test:
	$(PY) -m unittest discover -s tests -t . -v

test-hermes:
	@test -n "$(HERMES_AGENT_ROOT)" || (echo "set HERMES_AGENT_ROOT to a hermes-agent checkout" && exit 1)
	HERMES_AGENT_ROOT=$(HERMES_AGENT_ROOT) $(HERMES_AGENT_ROOT)/venv/bin/python -m unittest tests.test_plugin_load -v

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff format .
	ruff check --fix .

doctor:
	hermes plugins doctor . --ci
