# ai-spend-roi

Which teams' AI spend pays off, and where the next dollar of AI budget should go

A finance method that ties a company's AI spend, by team, to what each team produces. It reports a return only where a fair comparison group exists, says unmeasured and designs the experiment everywhere else, and allocates a stated budget to where measured marginal return is highest. It runs on synthetic data for a made-up company (Beta Co) and re-runs every month. The output is a static memo site in which every number links back to the source rows that produced it.

Status: work in progress. The commands below are the product's interface.

## What it answers

1. Does our AI spend pay off? Per team, with intervals, or unmeasured with the reason.
2. Where does the next dollar go? An allocation of a stated budget across the measured teams.
3. What should we test next? A designed experiment for each unmeasured team.

The memo page answers them in that order, and every figure on it is a link to a page that states the table, the predicate, the row count and the checksum behind it, with the rows themselves when there are few enough to show.

## What planted effects do and do not show

The data is synthetic. The generator plants a known effect of AI in each team, chosen within ranges taken from published studies, and writes the answer to a file that the method never reads. Only the verification command opens it. Some teams' planted effects come from studies of different work than the team's own, and the memo names them.

What recovering planted effects shows:

- The estimator, the return calculation and the allocation work as specified: given a company whose teams adopted AI tools at different times and one team that never did, the method returns each team's planted effect inside its stated interval, in the share of runs the interval level promises.
- The method refuses where it should: a team with no before period, too few months since rollout, no comparison team, or a trend that already differed before rollout is reported as unmeasured, with no return figure.
- The books tie to the ledger to the cent, and every number in the memo resolves to the rows that produced it.

What it does not show:

- It is not a finding about any real company, any real team or any real AI tool. Nothing here estimates how much AI helps anyone.
- It does not show that the comparison assumptions hold in real data. The generator is built so that comparison teams follow the same path as treated teams would have; a real company has to argue that.
- The effect sizes are the author's choices. The value of a unit of each team's output is an assumption, printed with a low and a high case and never presented as a finding.
- The allocation is computed from curves fitted to synthetic spend. It shows how the rule works, not where a real budget should go.

## Run it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Nothing touches the network after `uv sync`.

```
uv sync --frozen
uv run roi gen
uv run roi build
uv run roi verify --through 2026-03
uv run roi memo --month 2026-03 --check
uv run roi rollforward --month 2026-04 && uv run roi verify --through 2026-04
```

To try the machinery on a small made-up company that builds in seconds:

```
uv run roi build --toy --work-dir /tmp/toy
uv run roi memo --month 2025-06 --check --work-dir /tmp/toy
```

The memo is written to `site/` under the work directory. Money is kept in integer cents, and the same seed and commit give an identical warehouse and byte-identical memo HTML.

### Commands

| Command | What it does |
|---|---|
| `roi gen` | Generate the books and the answer file from the seed |
| `roi build` | Load the books, run the dbt models and tests, estimate, compute returns, curves, allocation, agenda and provenance |
| `roi verify --through <month>` | Ledger tie, interval coverage against planted effects, unmeasured discipline |
| `roi memo --month <month> [--check]` | Build the static site and, with `--check`, re-run every number's predicate and compare its row count and checksum |
| `roi explain <id>` | Print the provenance record behind one number (the id in its `data-prov` attribute) and re-check its rows |
| `roi signoff --month <month>` | A person records that they reviewed the month's memo. It refuses a non-interactive run |
| `roi doctor [--tree \| --release]` | Health check: lock file, books, warehouse, dbt tests, ledger tie, truth boundary. `--release` adds the checks that need `.git` |
| `roi allocate`, `roi agenda`, `roi estimate` | Print the allocation, the testing agenda and the team effects without writing anything |
| `roi rollforward --month <month>` | Add a month, re-verify, and swap the warehouse file |

Every command takes `--work-dir <dir>` and exits 0 when its checks pass, 1 when one fails, 2 for a usage error, 3 for a data integrity failure and 4 for a fail-closed configuration error.

## Tests

```
uv run pytest -q
```

## Verifying the pre-registration and sign-offs

The seed, the effects planted and the thresholds are fixed before the estimator is run, and their hash is in `config/prereg.sha256`. Each registration is a signed tag `prereg-v<n>`, and each monthly sign-off is a signed tag `signoff-<month>` on the commit that added `signoffs/<month>.yaml`. To check one:

```
git show <tag>:config/allowed-signers > signers
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=signers tag -v <tag>
git show prereg-v<n>:config/prereg.sha256
```

The last command prints the hash that `config/prereg.sha256` must equal at that tag. The signing keys are listed in `config/allowed-signers`, and only hardware-backed keys (`sk-` types) are accepted for sign-offs. A software key in that file makes `roi memo --require-signoff` fail.

Publishing key fingerprint: not yet published, because no signed tag exists yet. It is printed here when the first tag is signed; compare it with the key that `git tag -v` reports.

## License

MIT. See `LICENSE`.
