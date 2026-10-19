#!/usr/bin/env bash
# Definition of Done: the commands that must pass before a change is accepted.
# Run it on a clean `git archive HEAD` extraction to check the committed state only.
#
#   definition-of-done.sh --fast   what every change runs: shell syntax, the scrub-check tree
#                                  part and its tests, the fast pytest tests, and a toy build
#                                  (which runs the dbt models and their tests), then `roi dbt test` on it.
#   definition-of-done.sh          the fast tier plus the tests marked `slow`.
# Exit 2 when uv is absent.
set -euo pipefail

fast=0
case "${1-}" in
  "") ;;
  --fast) fast=1 ;;
  *) echo "usage: definition-of-done.sh [--fast]" >&2; exit 2 ;;
esac
[ $# -le 1 ] || { echo "usage: definition-of-done.sh [--fast]" >&2; exit 2; }

command -v uv >/dev/null 2>&1 || { echo "definition-of-done: uv is not installed" >&2; exit 2; }

cd "$(dirname "$0")/.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "== shell syntax"
bash -n scripts/*.sh scripts/hooks/pre-push
echo "== scrub-check, tree part"
scripts/scrub-check.sh --no-denylist
echo "== scrub-check tests"
bash tests/test_scrub_check.sh
echo "== pytest, not slow"
uv run --frozen pytest -q -m "not slow" -n 4
echo "== toy build and dbt tests"
uv run --frozen roi --work-dir "$work" build --toy
echo "== dbt test against the built warehouse"
uv run --frozen roi --work-dir "$work" dbt test

if [ "$fast" = 0 ]; then
  echo "== pytest, slow"
  rc=0
  uv run --frozen pytest -q -m slow || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ] || exit "$rc"   # 5: no slow tests exist yet
fi
echo "definition-of-done: checks passed"
