#!/usr/bin/env bash
# The 02:13 page, at record time: a macOS banner, then the PagerDuty message in
# #alerts with Ada's acknowledgement in its thread (scripts/demo/seed_slack.py
# --page) when a Slack bot token is configured, then the PagerDuty mail in the
# inbox (seed_gmail.py --page) with --mail.
# Slack is not in the current cut (docs/demo-script.md), so the Slack half is
# skipped, and said so, when SLACK_BOT_TOKEN is in neither the environment nor
# .env; it must not take the mail half down with it (measured 2026-09-24:
# without a token seed_slack.py exits 1, and under set -e that ended the script
# before the page mail was inserted).
# Staged on purpose: PagerDuty is not an integration, the page is a message.
set -euo pipefail
cd "$(dirname "$0")/../.."
osascript -e 'display notification "feedr-rev-a browned out in the field (3rd this month). On-call: Ada." with title "PagerDuty" subtitle "PAGE #4471 · 02:13 PDT" sound name "Sosumi"'
if [ -n "${SLACK_BOT_TOKEN:-}" ] || grep -qE '^SLACK_BOT_TOKEN=xoxb-' .env 2>/dev/null; then
  ./.venv/bin/python scripts/demo/seed_slack.py --page
else
  echo "slack: skipped, SLACK_BOT_TOKEN is not set (Slack is not in this cut)"
fi
if [ "${1:-}" = "--mail" ]; then ./.venv/bin/python scripts/demo/seed_gmail.py --page; fi
