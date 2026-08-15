# AI Knowledge Cockpit — dev shortcuts
#
# `make` shows the most common targets. Anything CI does should have a
# target here so devs and CI use the same commands.

PY     := .venv/bin/python
PIP    := .venv/bin/pip

.PHONY: help install test test-cov lint clean run-bot run-exam run-feishu run-wecom up down status logs restart push-tag

help:
	@echo "Common targets:"
	@echo "  make install    - install prod + dev deps"
	@echo "  make test       - run unit tests (unittest discover)"
	@echo "  make test-cov   - run tests with coverage report"
	@echo "  make lint       - syntax-check all .py modules"
	@echo "  make run-bot    - start the DingTalk bot (foreground)"
	@echo "  make run-feishu - start the Feishu bot (foreground)"
	@echo "  make run-wecom  - start the WeCom bot (foreground)"
	@echo "  make run-exam   - start the exam web app (foreground, http://127.0.0.1:5001)"
	@echo "  make up         - one-click: start bot + exam via launchd (background)"
	@echo "  make down       - stop both launchd services"
	@echo "  make status     - show running services + quick KB health"
	@echo "  make logs       - tail the last 50 lines of bot + exam logs"
	@echo "  make restart    - bounce both launchd services"
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

run-feishu:
	$(PY) -u app.py serve feishu

run-wecom:
	$(PY) -u app.py serve wecom

run-exam:
	$(PY) -u exam/app.py

# --- one-click service control (launchd) ---

up:
	@echo "starting DingTalk bot + exam app via launchd…"
	@launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist 2>/dev/null || launchctl kickstart -k gui/501/com.mavis.knowledge-bot
	@launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.mavis.exam-app.plist    2>/dev/null || launchctl kickstart -k gui/501/com.mavis.exam-app
	@sleep 2
	@make status

down:
	@echo "stopping launchd services…"
	@launchctl bootout gui/501/com.mavis.knowledge-bot 2>/dev/null || true
	@launchctl bootout gui/501/com.mavis.exam-app     2>/dev/null || true
	@echo "done."

status:
	@echo "=== launchd services ==="
	@launchctl list 2>/dev/null | grep -E "com\.mavis\.(knowledge-bot|exam-app)" || echo "  (none running)"
	@echo
	@echo "=== exam app health ==="
	@curl -sf http://127.0.0.1:5001/api/health 2>/dev/null | head -1 || echo "  (exam app not responding on 5001)"
	@echo
	@echo "=== KB stats ==="
	@$(PY) app.py status 2>&1 | head -8

logs:
	@tail -n 30 logs/dingtalk_bot.out.log 2>/dev/null
	@echo "---"
	@tail -n 30 logs/exam_app.out.log    2>/dev/null

restart:
	@launchctl kickstart -k gui/501/com.mavis.knowledge-bot
	@launchctl kickstart -k gui/501/com.mavis.exam-app
	@sleep 2
	@make status

push-tag:
	@if [ -z "$(TAG)" ]; then echo "usage: make push-tag TAG=v0.1.0"; exit 1; fi
	git tag -a "$(TAG)" -m "Release $(TAG)"
	git push origin "$(TAG)"

clean:
	rm -rf .pytest_cache .coverage htmlcov
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
