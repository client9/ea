
test:
	.venv/bin/python -m pytest tests/ -v

coverage:
	.venv/bin/python -m pytest tests/ --cov=ea --cov-report=term-missing

lint:
	ruff check --output-format=concise
format:
	ruff format


status:
	 python3 ea.py status

