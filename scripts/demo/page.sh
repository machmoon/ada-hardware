#!/usr/bin/env bash
# The 02:13 page, at record time: a macOS banner, then the PagerDuty message in
# #alerts with Ada's acknowledgement in its thread (scripts/demo/seed_slack.py
# --page), then the PagerDuty mail in the inbox (seed_gmail.py --page).
# Staged on purpose: PagerDuty is not an integration, the page is a message.
set -euo pipefail
cd "$(dirname "$0")/../.."
osascript -e 'display notification "feedr-rev-a browned out in the field (3rd this month). On-call: Ada." with title "PagerDuty" subtitle "PAGE #4471 · 02:13 PDT" sound name "Sosumi"'
./.venv/bin/python scripts/demo/seed_slack.py --page
if [ "${1:-}" = "--mail" ]; then ./.venv/bin/python scripts/demo/seed_gmail.py --page; fi
