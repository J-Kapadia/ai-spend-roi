#!/usr/bin/env bash
# The chain of trust for config/allowed-signers, and its test on throwaway repositories.
#
#   test_signers_chain.sh              run the checks below on fixture repos with fixture keys
#   test_signers_chain.sh --repo DIR   check the allowed-signers history of the repository at DIR
#                                      (needs .git; exit 1 on any problem, including no file yet)
#
# The rules checked by `chain_problems`:
#   - every key in every version of config/allowed-signers is a hardware-backed key type (`sk-`);
#   - the commit that first adds the file is signed by a key listed in that same version (the root
#     of trust is one human-made, hardware-signed commit);
#   - every later commit that changes the file is signed by a key listed in its first parent's
#     version of the file, so a key cannot add itself.
# Hardware keys cannot be made without a device, so the fixture repos below sign with software
# keys and run the checker with ROI_SIGNERS_ALLOW_SOFTWARE=1, which relaxes only the key-type rule.
# The type rule itself is tested with fixture lists. The throwaway repos live under a temp dir with
# no global git config and a temp HOME; nothing here touches a real key or repository.
set -uo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
SIGNERS=config/allowed-signers

# key_types FILE: the key type of each key in an allowed-signers file, one per line. A line is
# `principals [options] keytype base64 [comment]`, so the type is the second or third field and must
# be exactly a known type name; a principal named like a type is not read as one, and a line whose
# type cannot be read is reported as `unreadable`, which is never hardware-backed.
key_types() {
  awk '
    /^[[:space:]]*(#|$)/ { next }
    {
      type = "unreadable"
      for (i = 2; i <= 3 && i <= NF; i++)
        if ($i ~ /^(sk-)?(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp(256|384|521))(-cert-v01)?(@openssh\.com)?$/) { type = $i; break }
      print type
    }' "$1"
}

# type_problems FILE LABEL: print one problem per key that is not hardware-backed, or for a file with no key.
type_problems() {
  local types t
  types=$(key_types "$1")
  [ -n "$types" ] || { echo "$2 lists no keys"; return; }
  [ "${ROI_SIGNERS_ALLOW_SOFTWARE:-0}" = 1 ] && return
  while read -r t; do
    case $t in sk-*) ;; *) echo "$2 lists a key that is not hardware-backed ($t)" ;; esac
  done <<<"$types"
}

# chain_problems REPO: print one problem per line; no output means the chain holds.
chain_problems() {
  local repo=$1 commits c parent file_c file_p
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || { echo "$repo is not a git repository"; return; }
  commits=$(git -C "$repo" log --first-parent --reverse --format=%H -- "$SIGNERS")
  [ -n "$commits" ] || { echo "no commit has added $SIGNERS"; return; }
  local work; work=$(mktemp -d)
  for c in $commits; do
    file_c=$work/at-$c
    if ! git -C "$repo" show "$c:$SIGNERS" >"$file_c" 2>/dev/null; then
      echo "${c:0:12} deletes $SIGNERS"; continue
    fi
    type_problems "$file_c" "${c:0:12}"
    parent=$(git -C "$repo" rev-parse -q --verify "$c^" 2>/dev/null || true)
    file_p=
    if [ -n "$parent" ]; then
      file_p=$work/at-$parent
      git -C "$repo" show "$parent:$SIGNERS" >"$file_p" 2>/dev/null || file_p=
    fi
    local trusted=$file_p kind="its parent's list"
    if [ -z "$trusted" ]; then trusted=$file_c; kind="its own list (the root of trust)"; fi
    if ! git -C "$repo" -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$trusted" verify-commit "$c" >/dev/null 2>&1; then
      echo "${c:0:12} changes $SIGNERS but is not signed by a key in $kind"
    fi
  done
  rm -rf "$work"
}

if [ "${1-}" = --repo ]; then
  [ $# -eq 2 ] || { echo "usage: test_signers_chain.sh [--repo DIR]" >&2; exit 2; }
  out=$(chain_problems "$2")
  if [ -n "$out" ]; then printf 'signers chain: %s\n' "$out" >&2; exit 1; fi
  echo "signers chain: ok"
  exit 0
fi
[ $# -eq 0 ] || { echo "usage: test_signers_chain.sh [--repo DIR]" >&2; exit 2; }

# ---- fixtures --------------------------------------------------------------------------------------

command -v ssh-keygen >/dev/null || { echo "test_signers_chain: ssh-keygen is required" >&2; exit 2; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export HOME=$tmp/home GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "$HOME"

pass=0 failed=0
check() { # check <name> <expected exit> <actual exit>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); else failed=$((failed + 1)); echo "FAIL: $1 (expected exit $2, got $3)"; fi
}
has() { # has <name> <present|absent> <text>: checks $out
  if { [ "$2" = present ] && grep -qF -- "$3" <<<"$out"; } || { [ "$2" = absent ] && ! grep -qF -- "$3" <<<"$out"; }; then
    pass=$((pass + 1))
  else
    failed=$((failed + 1)); echo "FAIL: $1 (output should have $3 $2): $out"
  fi
}
run_chain() { out=$(chain_problems "$1" 2>&1); [ -z "$out" ]; rc=$?; }   # rc 0: the chain holds

for k in a b c; do ssh-keygen -q -t ed25519 -N '' -C "fixture $k" -f "$tmp/key-$k"; done
entry() { # entry <k>: the allowed-signers line for fixture key k
  echo "t@example.invalid namespaces=\"git\" $(cut -d' ' -f1,2 "$tmp/key-$1.pub")"
}
new_repo() { git init -q -b main "$1"; mkdir -p "$1/config"; echo seed > "$1/README"; git -C "$1" add -A; git -C "$1" commit -q -m init; }
commit_as() { # commit_as <repo> <key|none> <message>: commit the staged state, signed by fixture key or unsigned
  local repo=$1 key=$2
  git -C "$repo" add -A
  if [ "$key" = none ]; then
    git -C "$repo" commit -q -m "$3"
  else
    git -C "$repo" -c gpg.format=ssh -c "user.signingkey=$tmp/key-$key" commit -q -S -m "$3"
  fi
}
export ROI_SIGNERS_ALLOW_SOFTWARE=1   # the fixture keys are software keys; see the header

# ---- the chain of trust ----------------------------------------------------------------------------

r=$tmp/ok; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" a "root of trust"
run_chain "$r"; check "root commit signed by its own listed key passes" 0 $rc
entry b >> "$r/$SIGNERS"; commit_as "$r" a "add b"
run_chain "$r"; check "a change signed by a key in the parent's list passes" 0 $rc
entry c >> "$r/$SIGNERS"; commit_as "$r" b "add c, signed by the key added last time"
run_chain "$r"; check "a change signed by a key added by an earlier valid change passes" 0 $rc
grep -v "$(cut -d' ' -f2 "$tmp/key-a.pub")" "$r/$SIGNERS" > "$r/new" && mv "$r/new" "$r/$SIGNERS"; commit_as "$r" c "remove a"
run_chain "$r"; check "removing a key, signed by a listed key, passes" 0 $rc
echo unrelated > "$r/other"; commit_as "$r" none "an unsigned commit that leaves the list alone"
run_chain "$r"; check "an unsigned commit that does not touch the list is not checked" 0 $rc

r=$tmp/unsigned; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" a "root"
entry b >> "$r/$SIGNERS"; commit_as "$r" none "unsigned edit"
run_chain "$r"; check "an unsigned edit fails" 1 $rc
has "unsigned edit names the commit's problem" present "is not signed by a key in its parent's list"

r=$tmp/selfsigned; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" a "root"
entry c >> "$r/$SIGNERS"; commit_as "$r" c "c adds itself and signs with itself"
run_chain "$r"; check "a self-signed edit fails" 1 $rc

r=$tmp/stranger; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" a "root"
entry b >> "$r/$SIGNERS"; commit_as "$r" c "signed by a key that is nowhere in the list"
run_chain "$r"; check "an edit signed by an unlisted key fails" 1 $rc

r=$tmp/badroot; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" b "root signed by a key that is not in the file"
run_chain "$r"; check "a root commit signed by a key outside its own list fails" 1 $rc
has "bad root names the root of trust" present "the root of trust"

r=$tmp/unsignedroot; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" none "unsigned root"
run_chain "$r"; check "an unsigned root commit fails" 1 $rc

r=$tmp/deleted; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" a "root"
git -C "$r" rm -q "$SIGNERS"; commit_as "$r" a "delete the list"
run_chain "$r"; check "deleting the list fails" 1 $rc
has "deletion is named" present "deletes $SIGNERS"

r=$tmp/laterbad; new_repo "$r"
entry a > "$r/$SIGNERS"; commit_as "$r" a "root"
entry b >> "$r/$SIGNERS"; commit_as "$r" a "add b"
entry c >> "$r/$SIGNERS"; commit_as "$r" none "unsigned third edit"
entry a >> "$r/other"; commit_as "$r" a "a later signed commit does not repair history"
run_chain "$r"; check "one bad link anywhere in the history fails" 1 $rc

r=$tmp/nofile; new_repo "$r"
run_chain "$r"; check "a repository that never had the list fails" 1 $rc
has "no list is named" present "no commit has added $SIGNERS"
run_chain "$tmp/not-a-repo"; check "a directory that is not a repository fails" 1 $rc

# ---- the key-type rule (ROI_SIGNERS_ALLOW_SOFTWARE unset) ------------------------------------------

type_out() { ROI_SIGNERS_ALLOW_SOFTWARE=0 type_problems "$1" fixture; }
out=$(type_out "$here/tests/fixtures/allowed-signers"); [ -z "$out" ]; check "the hardware-key list is accepted" 0 $?
out=$(type_out "$here/tests/fixtures/allowed-signers.software"); [ -z "$out" ]; check "the software-key list is rejected" 1 $?
has "software key is named" present "not hardware-backed (ssh-ed25519)"
printf 'a@example.invalid namespaces="git" sk-ecdsa-sha2-nistp256@openssh.com AAAA\n' > "$tmp/ecdsa"
out=$(type_out "$tmp/ecdsa"); [ -z "$out" ]; check "the sk-ecdsa type is accepted" 0 $?
{ cat "$here/tests/fixtures/allowed-signers"; cat "$here/tests/fixtures/allowed-signers.software"; } > "$tmp/mixed"
out=$(type_out "$tmp/mixed"); [ -z "$out" ]; check "one software key among hardware keys is rejected" 1 $?
printf '# only a comment\n\n' > "$tmp/empty"
out=$(type_out "$tmp/empty"); [ -z "$out" ]; check "a list with no key is rejected" 1 $?
printf 'sk-ssh-ed25519@openssh.com namespaces="git" ssh-ed25519 AAAA\n' > "$tmp/lookalike"
out=$(type_out "$tmp/lookalike"); check "a principal named like a hardware key type does not pass a software key" 1 $([ -z "$out" ]; echo $?)
has "the real key type is the one reported" present "(ssh-ed25519)"
printf 'a@example.invalid namespaces="git" sk-nonsense AAAA\n' > "$tmp/garbled"
out=$(type_out "$tmp/garbled"); has "a line with an unknown key type is rejected as unreadable" present "(unreadable)"
printf 'a@example.invalid sk-ssh-ed25519@openssh.com AAAA\n' > "$tmp/nooptions"
out=$(type_out "$tmp/nooptions"); check "a line with no options is read" 0 $([ -z "$out" ]; echo $?)

# the whole chain under the real rule: software-signed fixture repo is refused for its key type
out=$(ROI_SIGNERS_ALLOW_SOFTWARE=0 chain_problems "$tmp/ok"); [ -z "$out" ]; check "a chain of software keys fails under the default rule" 1 $?

# --repo mode
bash "$0" --repo "$tmp/nofile" >/dev/null 2>&1; check "--repo fails where no list exists" 1 $?
ROI_SIGNERS_ALLOW_SOFTWARE=1 bash "$0" --repo "$tmp/ok" >/dev/null 2>&1; check "--repo passes a good chain" 0 $?
bash "$0" --repo >/dev/null 2>&1; check "--repo without a directory is a usage error" 2 $?

echo "test_signers_chain: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
