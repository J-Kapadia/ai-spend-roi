# Controls contract

What the code guarantees, who can change each guarantee, and the command or test that shows it
holds. A control that the party it constrains can edit is not a control, so each row names
where the check lives.

| Control | What it guarantees | Shown by | Who can change it |
|---|---|---|---|
| Ledger tie | AI spend by team and month equals the general-ledger AI-usage line to the cent. `roi memo`, `roi verify` and `roi doctor` recompute both sides from the raw tables with their own SQL, so a stale mart cannot hide a change. | `roi verify --through <month>`, `tests/test_verify.py`, `tests/test_memo.py` | Code review of `src/roi/verify_books.py` |
| Comparison discipline | A team without a valid comparison group is `unmeasured` with a reason code and has no ROI figure, interval or allocation. | `roi verify` group (c), `tests/test_verify.py` | Code review of the estimator and `verify_books` |
| Truth boundary | The estimator, ROI, allocation, agenda, memo, explain and doctor code never read the generator's answer file. Only `roi.verify_truth` does, and only `roi verify` and tests reach it. | `tests/test_truth_boundary.py`, run by `roi doctor` | Code review; the test fails when an import path appears |
| Provenance | Every number in the memo is tagged with a provenance id, linked to a page that states its source table, predicate, row count and checksum, and shows the rows when there are fewer than the page cap. | `roi memo --month <month> --check`, `roi explain <id>`, `tests/test_memo.py` | Code review of `src/roi/memo.py` and `src/roi/prov.py` |
| Determinism | The same seed and commit give byte-identical memo HTML. The memo carries no git sha or build time; build facts live beside the records, not in the page. | `tests/test_memo.py`, `tests/test_skeleton.py` | Code review |
| No network at run time | Nothing in the pipeline or the tests opens a socket. | the socket guard in `tests/conftest.py` | Code review of `tests/conftest.py` |
| Sign-off | A month's memo counts as reviewed only when `signoffs/<month>.yaml` was added by a commit signed by a hardware-backed key listed in `config/allowed-signers`, and the memo it names is the memo on disk. | `roi memo --month <month> --require-signoff`, `tests/test_signoff.py` | Only a commit signed by a key already listed may change the signers file |

## Sign-off in detail

`roi signoff --month <month>` writes `signoffs/<month>.yaml` with the reviewer's name, the date,
the content hash of the built site and the provenance ids of the numbers reviewed. It refuses to
run without a person at a terminal and it never overwrites a sign-off. The file proves nothing by
itself, because anyone can write one.

The control is the commit that adds the file. `roi memo --require-signoff` checks, in this order:

1. the file exists and its memo hash equals the hash of the site on disk;
2. every key in `config/allowed-signers` is a hardware-backed key type (`sk-ssh-ed25519@openssh.com`
   or `sk-ecdsa-sha2-nistp256@openssh.com`), so a software key cannot be listed;
3. the commit that added the file is signed (SSH) by a listed key, and the file is unchanged since.

The check needs `.git` and exits 4 without it. The memo HTML carries no commit id, so committing
the sign-off does not change the memo it attests.

A pending sign-off is allowed by a plain `roi memo --check`, which reports it. Only
`--require-signoff` fails on it.

## What the provenance record holds

A record is a named table, a SQL predicate bounded at the memo month, a row count and a checksum
over the rows the predicate returns. Its id is the first 16 hex characters of a SHA-256 over the
name, month, table, predicate and displayed value, so the same number always has the same id.

Two kinds of record exist today. The headline figure's record is written by `roi build` into
`prov_record`, with the build facts (code version, producer, checks passed) in the append-only
`prov_build`. The records of the figures from the result tables, and the assumption numbers read
from `config/roi.yaml`, are derived from the warehouse each time the memo is built or checked, with
the same id function. `roi explain` says which kind it is printing.

The consequence of derivation is that the check for a derived figure compares the page with the
warehouse as it is now. It catches a page edited by hand, a page built from older results, and a
changed config value. It does not catch a result row edited before the site is rebuilt; the
ledger tie, `roi verify` and the stored headline record are what guard the warehouse itself.

## Reporting a failure

Every command exits 0 when all checks pass, 1 when a check fails (an unresolved number, a
checksum mismatch, a failed doctor check), 2 for a usage or precondition error, 3 for a data
integrity failure (the ledger tie), and 4 for a fail-closed configuration error (a signature check
with no `.git`).
