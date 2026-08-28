.PHONY: test smoke install-agent lint

test:
	cd travel/bond-agent && python3 -m pytest -q

smoke:
	./scripts/smoke-test.sh

install-agent:
	cd travel/bond-agent && pip3 install -e .

lint:
	cd travel/bond-agent && python3 -m ruff check zippie tests || true
