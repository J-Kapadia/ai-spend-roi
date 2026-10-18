#!/usr/bin/env bash
# Re-register the generator seed: scripts/reregister-seed.sh <new-seed> "<why>"
#                  or a change to the data-generating keys at the same seed:
#                                  scripts/reregister-seed.sh --dgp-change "<why>"
#
# The first form changes the seed in config/gen.yaml, rewrites config/prereg.sha256 and appends
# one line to config/prereg.log. The second takes a ramp, noise or other data-generating key you
# edited in config/gen.yaml by hand, keeps the seed, and logs the hash of those keys before and
# after. Both regenerate the books into a temporary directory and rewrite data/MANIFEST.sha256 and
# data/MANIFEST.incoming.sha256 from them, so one commit holds the whole registration. Neither
# creates a tag; the signed tag prereg-v<n+1> is the author's to create. The budget is the first
# registration plus three re-registrations per estimator version, shared by both forms; a spent
# budget, a registered input edited by hand (other than for --dgp-change), a seed that is not new,
# or a --dgp-change with no change exits 3.
#
# <why> says the old seed, the failing team and the numbers, and whether the miss was a
# coverage miss or a pre-trend flag; for --dgp-change, which key moved and why.
#
# Environment (tests): ROI_PREREG_ROOT is the directory holding config/ (default: this
# repository); ROI_PREREG_PYTHON is the interpreter command (default: uv run --frozen python).
# Exit 0 done, 2 usage, 3 refused.
set -euo pipefail

usage() { echo 'usage: reregister-seed.sh <new-seed> "<why>" | reregister-seed.sh --dgp-change "<why>"' >&2; exit 2; }
[ $# -eq 2 ] || usage
why=$2
if [ "$1" = "--dgp-change" ]; then
  mode=(--dgp-change)
else
  seed=$1
  [[ $seed =~ ^[1-9][0-9]*$ ]] || { echo "reregister-seed: the new seed must be a positive integer" >&2; exit 2; }
  mode=(--new-seed "$seed")
fi
[ -n "${why//[[:space:]]/}" ] || { echo "reregister-seed: say why" >&2; exit 2; }

here=$(cd "$(dirname "$0")/.." && pwd)
root=${ROI_PREREG_ROOT:-$here}
if [ -n "${ROI_PREREG_PYTHON:-}" ]; then
  py=("$ROI_PREREG_PYTHON")
else
  command -v uv >/dev/null 2>&1 || { echo "reregister-seed: uv is not installed" >&2; exit 2; }
  py=(uv run --frozen --project "$here" python)
fi

exec "${py[@]}" -m roi.prereg --root "$root" reregister "${mode[@]}" --why "$why"
