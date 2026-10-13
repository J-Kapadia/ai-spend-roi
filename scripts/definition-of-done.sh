#!/usr/bin/env bash
# Definition of Done: the commands that must pass before a bead may close.
# NEEDLE runs this on a clean `git archive HEAD` extraction (committed state only).
set -euo pipefail
cd "$(dirname "$0")/.."
bash -n scripts/*.sh
echo "definition-of-done: checks passed"
