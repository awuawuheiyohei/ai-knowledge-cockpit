# AI Knowledge Cockpit — dev shortcuts
#
# `make` shows the most common targets. Anything CI does should have a
# target here so devs and CI use the same commands.

PY     := .venv/bin/python
PIP    := .venv/bin/pip

.PHONY: help install test test-cov lint clean run-bot run-exam push-tag

help:
	@echo "Common targets:"
	@echo "  make install    - install prod + dev deps"
	@echo "  make test       - run unit tests (unittest discover)"
	@echo "  make test-cov   - run tests with coverage report"
	@echo "  make lint       - syntax-check all .py modules"
	@echo "  make run-bot    - start the DingTalk bot (foreground)"
	@echo "  make run-exam   - start the exam web app (foreground)"
	@echo "  make push-tag   - cut a release tag and push to origin"

install:
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt

test:
	$(PY) -m unittest discover -s tests -p "test_*.py" -v

test-cov:
	$(PY) -m coverage run -m unittest discover -s tests -p "test_*.py"
	$(PY) -m coverage report -m --skip-empty

lint:
	@for f in *.py exam/*.py tools/*.py; do \
	  $(PY) -m py_compile "$$f" || exit 1; \
	done
	@echo "all modules compile"

run-bot:
	$(PY) -u app.py serve dingtalk

run-exam:
	$(PY) -u exam/app.py

push-tag:
	@if [ -z "$(TAG)" ]; then echo "usage: make push-tag TAG=v0.1.0"; exit 1; fi
	git tag -a "$(TAG)" -m "Release $(TAG)"
	git push origin "$(TAG)"

clean:
	rm -rf .pytest_cache .coverage htmlcov
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
