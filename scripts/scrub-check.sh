#!/usr/bin/env bash
# Scrub check: keep secrets, private paths and private terms out of this public repository.
#
#   scrub-check.sh --no-denylist   tree part: secret patterns over every file, and a check that no
#                                  private path is tracked (the private paths are listed below).
#                                  Needs no denylist and no .git, so it runs on a bare extraction.
#   scrub-check.sh --history       history part: secret patterns over every commit message on all
#                                  refs. Exits 4 without .git.
#   scrub-check.sh                 everything: the tree part, the history part (when .git exists)
#                                  and the denylist over every file and commit message.
#   --root <dir>                   check <dir> instead of this repository.
#   --exclude <path>               skip a file (repeatable).
#   --list-private                 print the private paths, one per line, and exit.
#
# The denylist is the file named by $ROI_DENYLIST_FILE, or $HOME/.needle/ai-spend-roi.denylist
# when that variable is unset or empty: one extended regular expression per line, kept outside
# every checkout. Output names files, lines and commits and never prints a term or a matched line.
# The private paths are the lines of the file named by $ROI_PRIVATE_PATHS_FILE when it is set;
# otherwise the "# Private:" block of the repository's .git/info/exclude. A path ending in "/" is a
# directory. With neither source the private-path check is reported as skipped.
# Exit: 0 clean, 1 hit, 2 usage, 4 cannot check (denylist missing, unreadable, empty or
# invalid; no .git for --history; private paths file unreadable): fails closed.
set -euo pipefail

usage() {
  echo "usage: scrub-check.sh [--no-denylist] [--history] [--list-private] [--root <dir>] [--exclude <path>]..." >&2
  exit 2
}

do_tree=0 do_history=0 no_denylist=0 list_private=0 root="" excludes=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-denylist) do_tree=1; no_denylist=1 ;;
    --history) do_history=1 ;;
    --list-private) list_private=1 ;;
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

denylist=${ROI_DENYLIST_FILE:-${HOME-}/.needle/ai-spend-roi.denylist}
case "$denylist" in /*) ;; *) denylist=$PWD/$denylist ;; esac
private_file=${ROI_PRIVATE_PATHS_FILE-}
case "$private_file" in "" | /*) ;; *) private_file=$PWD/$private_file ;; esac
if [ -z "$root" ]; then root=$(dirname "$0")/..; fi
[ -d "$root" ] || { echo "scrub-check: $root is not a directory" >&2; exit 2; }
cd "$root"

has_git=0
[ -e .git ] && has_git=1
[ -z "$private_file" ] || [ -r "$private_file" ] || { echo "scrub-check: the private paths file is not readable" >&2; exit 4; }

# private_paths: print the private paths, one per line, from $ROI_PRIVATE_PATHS_FILE or the
# "# Private:" block of the repository's exclude file. Prints nothing when there is no source.
private_paths() {
  local src=""
  if [ -n "$private_file" ]; then
    src=$private_file
  elif [ "$has_git" = 1 ]; then
    local common
    common=$(git rev-parse --git-common-dir 2>/dev/null) || return 0
    [ -f "$common/info/exclude" ] && src=$common/info/exclude
  fi
  [ -n "$src" ] || return 0
  if [ -n "$private_file" ]; then
    grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$src" | sed 's|^/||' || true
  else
    awk '/^# Private:/ {on=1; next} on && NF && !/^#/ {print}' "$src" | sed 's|^/||'
  fi
}

if [ "$list_private" = 1 ]; then
  private_paths
  exit 0
fi

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

  mapfile -t private < <(private_paths)
  if [ "${#private[@]}" -eq 0 ]; then
    echo "scrub-check: no private paths listed; the private-path check is skipped"
  else
    for f in "${listed[@]}"; do
      f=${f#./}
      for p in "${private[@]}"; do
        case "$p" in
          */) [[ $f == "$p"* ]] && { echo "scrub-check: private path is tracked: $f"; fail=1; } ;;
          *) [ "$f" = "$p" ] && { echo "scrub-check: private path is tracked: $f"; fail=1; } ;;
        esac
      done
    done
  fi
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
  [ -r "$denylist" ] || { echo "scrub-check: denylist $denylist is not readable; refusing to pass" >&2; exit 4; }
  terms=$(mktemp)
  trap 'rm -f "$terms"' EXIT
  grep -v -e '^[[:space:]]*$' "$denylist" > "$terms" || true
  [ -s "$terms" ] || { echo "scrub-check: denylist $denylist has no terms; refusing to pass" >&2; exit 4; }
  grep -E -f "$terms" /dev/null 2>/dev/null || [ $? -eq 1 ] || { echo "scrub-check: denylist $denylist has an invalid pattern; refusing to pass" >&2; exit 4; }
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
