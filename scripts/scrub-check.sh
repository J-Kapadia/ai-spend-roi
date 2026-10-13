#!/usr/bin/env bash
# Scrub check: keep secrets, private paths and private terms out of this public repository.
#
#   scrub-check.sh --no-denylist   tree part: secret patterns over every file, and a check that no
#                                  path from the .gitignore Private block is tracked. Needs no
#                                  denylist and no .git, so it runs on a bare extraction.
#   scrub-check.sh --history       history part: secret patterns over every commit message on all
#                                  refs. Exits 4 without .git.
#   scrub-check.sh                 everything: the tree part, the history part (when .git exists)
#                                  and the denylist over every file and commit message.
#   --root <dir>                   check <dir> instead of this repository.
#   --exclude <path>               skip a file (repeatable).
#
# The denylist is $ROI_DENYLIST_FILE (default $HOME/.needle/ai-spend-roi.denylist), one extended
# regular expression per line. Output names files, lines and commits and never prints a term or a
# matched line.
# Exit: 0 clean, 1 hit, 2 usage, 4 cannot check (denylist unreadable or empty or invalid, no .git
# for --history, no Private block in .gitignore): fails closed.
set -euo pipefail

usage() {
  echo "usage: scrub-check.sh [--no-denylist] [--history] [--root <dir>] [--exclude <path>]..." >&2
  exit 2
}

do_tree=0 do_history=0 no_denylist=0 root="" excludes=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-denylist) do_tree=1; no_denylist=1 ;;
    --history) do_history=1 ;;
    --root) [ $# -ge 2 ] || usage; root=$2; shift ;;
    --exclude) [ $# -ge 2 ] || usage; excludes+=("${2#./}"); shift ;;
    *) usage ;;
  esac
  shift
done
explicit_history=$do_history
if [ "$do_tree" = 0 ] && [ "$do_history" = 0 ]; then
  do_tree=1 do_history=1
fi
do_denylist=0
[ "$no_denylist" = 0 ] && [ "$do_tree" = 1 ] && do_denylist=1

denylist=${ROI_DENYLIST_FILE:-$HOME/.needle/ai-spend-roi.denylist}
case "$denylist" in /*) ;; *) denylist=$PWD/$denylist ;; esac
if [ -z "$root" ]; then root=$(dirname "$0")/..; fi
[ -d "$root" ] || { echo "scrub-check: $root is not a directory" >&2; exit 2; }
cd "$root"

has_git=0
[ -e .git ] && has_git=1

SECRET_RE=(
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'gh[pousr]_[A-Za-z0-9]{36,}'
  'github_pat_[A-Za-z0-9_]{40,}'
  'sk-[A-Za-z0-9_-]{32,}'
  'xox[abprs]-[A-Za-z0-9-]{10,}'
  '(api[_-]?key|secret|token|passw(or)?d)[A-Za-z_]*["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9/+_.=-]{20,}["'"'"']'
)
secret_args=()
for re in "${SECRET_RE[@]}"; do secret_args+=(-e "$re"); done

fail=0
ran_tree="not run" ran_history="not run" ran_denylist="not run"

# Files to scan: the tracked files, or every file when there is no .git (an extraction).
if [ "$has_git" = 1 ]; then
  mapfile -d '' listed < <(git ls-files -z)
else
  mapfile -d '' listed < <(find . -path ./.git -prune -o -type f -print0)
fi
FILES=()
for f in "${listed[@]}"; do
  f=${f#./}
  [ -f "$f" ] || continue
  skip=0
  for x in "${excludes[@]+"${excludes[@]}"}"; do [ "$f" = "$x" ] && skip=1; done
  [ "$skip" = 1 ] || FILES+=("$f")
done

# scan_files <grep args>: print file:line for each match, never the line itself.
scan_files() {
  [ "${#FILES[@]}" -gt 0 ] || return 0
  printf '%s\0' "${FILES[@]}" | xargs -0 grep -InHE "$@" 2>/dev/null | cut -d: -f1,2 || true
}

if [ "$do_tree" = 1 ]; then
  ran_tree=run
  hits=$(scan_files "${secret_args[@]}")
  if [ -n "$hits" ]; then
    echo "$hits" | sed 's/^/scrub-check: secret pattern at /'
    fail=1
  fi

  [ -f .gitignore ] || { echo "scrub-check: no .gitignore; cannot read the Private block" >&2; exit 4; }
  mapfile -t private < <(awk '/^# Private:/ {on=1; next} on && NF && !/^#/ {print}' .gitignore)
  [ "${#private[@]}" -gt 0 ] || { echo "scrub-check: .gitignore has no Private block" >&2; exit 4; }
  for f in "${listed[@]}"; do
    f=${f#./}
    for p in "${private[@]}"; do
      p=${p#/}
      case "$p" in
        */) [[ $f == "$p"* ]] && { echo "scrub-check: private path is tracked: $f"; fail=1; } ;;
        *) [ "$f" = "$p" ] && { echo "scrub-check: private path is tracked: $f"; fail=1; } ;;
      esac
    done
  done
fi

# messages_matching <grep args>: print the id of each commit, on all refs, whose message matches.
messages_matching() {
  local c
  while read -r c; do
    if git log -1 --format=%B "$c" | grep -qE "$@" 2>/dev/null; then echo "$c"; fi
  done < <(git rev-list --all)
}

if [ "$do_history" = 1 ]; then
  if [ "$has_git" = 0 ]; then
    if [ "$explicit_history" = 1 ]; then
      echo "scrub-check: --history needs a .git directory" >&2
      exit 4
    fi
  else
    ran_history=run
    hits=$(messages_matching "${secret_args[@]}")
    if [ -n "$hits" ]; then
      echo "$hits" | sed 's/^/scrub-check: secret pattern in the message of commit /'
      fail=1
    fi
  fi
fi

if [ "$do_denylist" = 1 ]; then
  [ -r "$denylist" ] || { echo "scrub-check: denylist is not readable; refusing to pass" >&2; exit 4; }
  terms=$(mktemp)
  trap 'rm -f "$terms"' EXIT
  grep -v -e '^[[:space:]]*$' "$denylist" > "$terms" || true
  [ -s "$terms" ] || { echo "scrub-check: denylist has no terms; refusing to pass" >&2; exit 4; }
  grep -E -f "$terms" /dev/null 2>/dev/null || [ $? -eq 1 ] || { echo "scrub-check: denylist has an invalid pattern; refusing to pass" >&2; exit 4; }
  ran_denylist=run
  hits=$(scan_files -f "$terms")
  if [ -n "$hits" ]; then
    echo "$hits" | sed 's/^/scrub-check: denylisted term (not shown) at /'
    fail=1
  fi
  if [ "$has_git" = 1 ]; then
    hits=$(messages_matching -f "$terms")
    if [ -n "$hits" ]; then
      echo "$hits" | sed 's/^/scrub-check: denylisted term (not shown) in the message of commit /'
      fail=1
    fi
  fi
fi

echo "scrub-check: tree: $ran_tree; history: $ran_history; denylist: $ran_denylist"
if [ "$fail" != 0 ]; then
  echo "scrub-check: FAILED" >&2
  exit 1
fi
echo "scrub-check: ok"
