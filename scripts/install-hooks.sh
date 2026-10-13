#!/usr/bin/env bash
# Point git at the versioned hooks in scripts/hooks. Run once per clone.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -e .git ] || { echo "install-hooks: not a git checkout" >&2; exit 1; }
chmod +x scripts/hooks/*
git config core.hooksPath scripts/hooks
echo "install-hooks: core.hooksPath = $(git config core.hooksPath)"
