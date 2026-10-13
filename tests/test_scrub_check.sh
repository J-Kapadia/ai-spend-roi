#!/usr/bin/env bash
# Tests for scripts/scrub-check.sh and scripts/hooks/pre-push, on fixture trees and repos.
# Uses a synthetic denylist in a temp file and a temp HOME: never the real denylist.
set -uo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export HOME=$tmp/home GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "$HOME"
unset ROI_DENYLIST_FILE

SCRUB=$here/scripts/scrub-check.sh
TERM_WORD=zebrafoxtrot7                       # synthetic denylisted term
SECRET="AKIA$(printf 'B%.0s' {1..16})"        # built at run time so this file holds no pattern match
printf '%s\n' "$TERM_WORD" > "$tmp/denylist"
printf '' > "$tmp/empty"

pass=0 failed=0
check() { # check <name> <expected exit> <actual exit>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); else failed=$((failed + 1)); echo "FAIL: $1 (expected exit $2, got $3)"; fi
}
check_out() { # check_out <name> <present|absent> <text>   (checks $out)
  if { [ "$2" = present ] && grep -qF -- "$3" <<<"$out"; } || { [ "$2" = absent ] && ! grep -qF -- "$3" <<<"$out"; }; then
    pass=$((pass + 1))
  else
    failed=$((failed + 1)); echo "FAIL: $1 (output should have $3 $2)"
  fi
}
run() { out=$("$@" 2>&1); rc=$?; }

GITIGNORE='*.log

# Private: versioned elsewhere
/docs/plan/
/docs/research/
/notes/
/AGENTS.md
'

new_tree() { # new_tree <dir>: a clean fixture tree
  mkdir -p "$1/src"
  printf '%s' "$GITIGNORE" > "$1/.gitignore"
  echo 'print("hello")' > "$1/src/a.py"
}
new_repo() { # new_repo <dir>: the same tree as a committed git repository
  new_tree "$1"
  git -C "$1" init -q -b main && git -C "$1" add -A && git -C "$1" commit -q -m "initial"
}

# --- tree part, extraction (no .git)
new_tree "$tmp/t1"
run "$SCRUB" --no-denylist --root "$tmp/t1"
check "clean tree passes" 0 $rc
check_out "tree printed as run" present "tree: run"
check_out "history printed as not run" present "history: not run"
check_out "denylist printed as not run" present "denylist: not run"

new_tree "$tmp/t2"; printf 'key = %s\n' "$SECRET" > "$tmp/t2/src/b.py"
run "$SCRUB" --no-denylist --root "$tmp/t2"
check "planted secret fails" 1 $rc
check_out "secret located by file" present "src/b.py:1"
check_out "secret never printed" absent "$SECRET"

run "$SCRUB" --no-denylist --root "$tmp/t2" --exclude src/b.py
check "excluded file is skipped" 0 $rc

new_tree "$tmp/t3"; mkdir -p "$tmp/t3/docs/research"; echo note > "$tmp/t3/docs/research/n.md"
run "$SCRUB" --no-denylist --root "$tmp/t3"
check "private path present in an extraction fails" 1 $rc
check_out "private path named" present "docs/research/n.md"

new_tree "$tmp/t4"; rm "$tmp/t4/.gitignore"
run "$SCRUB" --no-denylist --root "$tmp/t4"
check "missing .gitignore fails closed" 4 $rc
printf '*.log\n' > "$tmp/t4/.gitignore"
run "$SCRUB" --no-denylist --root "$tmp/t4"
check "missing Private block fails closed" 4 $rc

# --- tree part, git repository
new_repo "$tmp/r1"
mkdir -p "$tmp/r1/notes"; echo private > "$tmp/r1/notes/x.md"
run "$SCRUB" --no-denylist --root "$tmp/r1"
check "untracked gitignored private path passes" 0 $rc
git -C "$tmp/r1" add -f notes/x.md
run "$SCRUB" --no-denylist --root "$tmp/r1"
check "tracked private path fails" 1 $rc
check_out "tracked private path named" present "notes/x.md"

new_repo "$tmp/r2"; mkdir -p "$tmp/r2/docs/research"; echo n > "$tmp/r2/docs/research/n.md"
git -C "$tmp/r2" add -f docs/research/n.md
run "$SCRUB" --no-denylist --root "$tmp/r2"
check "tracked file under docs/research fails" 1 $rc

# --- history part
run "$SCRUB" --history --root "$tmp/t1"
check "--history without .git fails closed" 4 $rc
new_repo "$tmp/h1"
run "$SCRUB" --history --root "$tmp/h1"
check "clean history passes" 0 $rc
check_out "history printed as run" present "history: run"
check_out "tree printed as not run" present "tree: not run"
echo x > "$tmp/h1/x"; git -C "$tmp/h1" add x; git -C "$tmp/h1" commit -q -m "add key $SECRET"
run "$SCRUB" --history --root "$tmp/h1"
check "secret in a commit message fails" 1 $rc
check_out "commit message secret never printed" absent "$SECRET"

# --- denylist (full run)
run env ROI_DENYLIST_FILE="$tmp/denylist" "$SCRUB" --root "$tmp/r2" --exclude docs/research/n.md
check "--exclude does not hide a tracked private path" 1 $rc
new_repo "$tmp/d1"
run env ROI_DENYLIST_FILE="$tmp/denylist" "$SCRUB" --root "$tmp/d1"
check "clean repo passes the denylist" 0 $rc
check_out "denylist printed as run" present "denylist: run"

echo "contains $TERM_WORD here" > "$tmp/d1/src/c.py"; git -C "$tmp/d1" add -A; git -C "$tmp/d1" commit -q -m "add c"
run env ROI_DENYLIST_FILE="$tmp/denylist" "$SCRUB" --root "$tmp/d1"
check "denylisted term in a file fails" 1 $rc
check_out "file located" present "src/c.py:1"
check_out "term never printed (file)" absent "$TERM_WORD"
run env ROI_DENYLIST_FILE="$tmp/denylist" "$SCRUB" --no-denylist --root "$tmp/d1"
check "--no-denylist ignores the denylist" 0 $rc

new_repo "$tmp/d2"; echo y > "$tmp/d2/y"; git -C "$tmp/d2" add y; git -C "$tmp/d2" commit -q -m "mention $TERM_WORD"
run env ROI_DENYLIST_FILE="$tmp/denylist" "$SCRUB" --root "$tmp/d2"
check "denylisted term in a commit message fails" 1 $rc
check_out "term never printed (message)" absent "$TERM_WORD"

new_tree "$tmp/d3"; echo "$TERM_WORD" > "$tmp/d3/f"
run env ROI_DENYLIST_FILE="$tmp/denylist" "$SCRUB" --root "$tmp/d3"
check "denylisted term in an extraction fails" 1 $rc

run env ROI_DENYLIST_FILE="$tmp/missing" "$SCRUB" --root "$tmp/d1"
check "missing denylist exits 4" 4 $rc
run env ROI_DENYLIST_FILE="$tmp/empty" "$SCRUB" --root "$tmp/d1"
check "empty denylist exits 4" 4 $rc
printf '[unclosed\n' > "$tmp/bad"
run env ROI_DENYLIST_FILE="$tmp/bad" "$SCRUB" --root "$tmp/d1"
check "invalid denylist pattern exits 4" 4 $rc
run "$SCRUB" --root "$tmp/d1"
check "default denylist path absent (temp HOME) exits 4" 4 $rc

# --- usage
run "$SCRUB" --bogus
check "unknown option exits 2" 2 $rc
run "$SCRUB" --root "$tmp/does-not-exist"
check "missing root exits 2" 2 $rc

# --- pre-push hook, in a fixture clone with a local bare remote
mk_clone() { # mk_clone <name>: fixture repo holding this repository's scripts, hooks installed
  local d=$tmp/$1
  new_tree "$d"; cp -r "$here/scripts" "$d/scripts"
  git -C "$d" init -q -b main
  git init -q --bare "$tmp/$1.git"; git -C "$d" remote add origin "$tmp/$1.git"
  git -C "$d" add -A; git -C "$d" commit -q -m "initial"
  "$d/scripts/install-hooks.sh" >/dev/null
}
mk_clone p1
check "install-hooks sets core.hooksPath" scripts/hooks "$(git -C "$tmp/p1" config core.hooksPath)"
run env ROI_DENYLIST_FILE="$tmp/denylist" git -C "$tmp/p1" push -q origin main
check "hook allows a clean push" 0 $rc
run env ROI_DENYLIST_FILE="$tmp/missing" git -C "$tmp/p1" push -q origin main
check "hook refuses when the denylist is missing" 1 $rc

echo "x $TERM_WORD" > "$tmp/p1/src/t.py"; git -C "$tmp/p1" add -A; git -C "$tmp/p1" commit -q -m "term"
run env ROI_DENYLIST_FILE="$tmp/denylist" git -C "$tmp/p1" push -q origin main
check "hook refuses a denylisted term" 1 $rc
check_out "hook never prints the term" absent "$TERM_WORD"
git -C "$tmp/p1" reset -q --hard HEAD~1

mkdir -p "$tmp/p1/docs/plan"; echo p > "$tmp/p1/docs/plan/p.md"; git -C "$tmp/p1" add -f docs/plan/p.md; git -C "$tmp/p1" commit -q -m "plan"
run env ROI_DENYLIST_FILE="$tmp/denylist" git -C "$tmp/p1" push -q origin main
check "hook refuses a private path" 1 $rc
git -C "$tmp/p1" reset -q --hard HEAD~1

printf 'k = %s\n' "$SECRET" > "$tmp/p1/src/k.py"; git -C "$tmp/p1" add -A; git -C "$tmp/p1" commit -q -m "key"
run env ROI_DENYLIST_FILE="$tmp/denylist" git -C "$tmp/p1" push -q origin main
check "hook refuses a secret (tree part)" 1 $rc
git -C "$tmp/p1" reset -q --hard HEAD~1

echo z > "$tmp/p1/z"; git -C "$tmp/p1" add z; git -C "$tmp/p1" commit -q -m "mention $TERM_WORD"
run env ROI_DENYLIST_FILE="$tmp/denylist" git -C "$tmp/p1" push -q origin main
check "hook refuses a term in a commit message" 1 $rc

echo "test_scrub_check: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
