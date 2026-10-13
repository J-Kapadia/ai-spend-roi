#!/usr/bin/env bash
# Re-register the generator seed: scripts/reregister-seed.sh <new-seed> "<why>"
#
# Changes the seed in config/gen.yaml, rewrites config/prereg.sha256 and appends one line to
# config/prereg.log. It creates no tag; the signed tag prereg-v<n+1> is the author's to create.
# The budget is the first registration plus three re-registrations per estimator version; a
# spent budget, a registered input edited by hand, or a seed that is not new exits 3.
#
# <why> says the old seed, the failing team and the numbers, and whether the miss was a
# coverage miss or a pre-trend flag.
#
# Environment (tests): ROI_PREREG_ROOT is the directory holding config/ (default: this
# repository); ROI_PREREG_PYTHON is the interpreter command (default: uv run --frozen python).
# Exit 0 done, 2 usage, 3 refused.
set -euo pipefail

usage() { echo 'usage: reregister-seed.sh <new-seed> "<why>"' >&2; exit 2; }
[ $# -eq 2 ] || usage
seed=$1 why=$2
[[ $seed =~ ^[1-9][0-9]*$ ]] || { echo "reregister-seed: the new seed must be a positive integer" >&2; exit 2; }
[ -n "${why//[[:space:]]/}" ] || { echo "reregister-seed: say why" >&2; exit 2; }

here=$(cd "$(dirname "$0")/.." && pwd)
root=${ROI_PREREG_ROOT:-$here}
if [ -n "${ROI_PREREG_PYTHON:-}" ]; then
  py=("$ROI_PREREG_PYTHON")
else
  command -v uv >/dev/null 2>&1 || { echo "reregister-seed: uv is not installed" >&2; exit 2; }
  py=(uv run --frozen --project "$here" python)
fi

exec "${py[@]}" -m roi.prereg --root "$root" reregister --new-seed "$seed" --why "$why"
