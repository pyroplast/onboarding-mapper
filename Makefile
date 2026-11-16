.PHONY: install test lint bench speed visual examples demo docker docker-test

install:
	pip install -e ".[dev]"

test:
	python -m pytest

lint:
	ruff check src tests bench examples && ruff format --check src tests bench examples

bench:
	python bench/evaluate.py

speed:
	python bench/speed.py

visual:
	python bench/visual.py

examples:
	python examples/generate.py

demo:
	omap map examples/exports/nomina_es.csv --out out/nomina_es --as-of 2026-11-16 --default-country ES

docker:
	docker build -t onboarding-mapper .

docker-test: docker
	docker run --rm --entrypoint python onboarding-mapper -m pytest -q
