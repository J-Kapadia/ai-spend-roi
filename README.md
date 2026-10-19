# ai-spend-roi

Which teams' AI spend pays off, and where the next dollar of AI budget should go

A finance method that ties a company's AI spend, by team, to what each team produces. It reports a return only where a fair comparison group exists, says unmeasured and designs the experiment everywhere else, and allocates a stated budget to where measured marginal return is highest. It runs on synthetic data for a made-up company (Beta Co) and re-runs every month. The output is a static memo site in which every number links back to the source rows that produced it.

Status: work in progress. The commands below are the product's interface.

## What it answers

1. Does our AI spend pay off? Per team, with intervals, or unmeasured with the reason.
2. Where does the next dollar go? An allocation of a stated budget across the measured teams, capped at 50% beyond each team's highest observed spend, with an experiment before budget for a team whose curve is not identified.
3. What should we test next? A designed experiment for each unmeasured team, with a note on what limits it.

The memo page answers them in that order, and every figure on it is a link to a page that states the table, the predicate, the row count and the checksum behind it, with the rows themselves when there are few enough to show.

## What the memo shows

`roi memo` writes `site/index.html` and one page per team, `site/teams/<team_id>.html`, beside it. The index lists a link to every team page. A team page repeats the planted-effect banner and gives that team's output metric, status and reason, effect and interval, return row with its verdict, sensitivity row, allocation row and testing agenda item. Every figure on a team page is the same number as on the index and links to the same provenance page, so no team page computes anything of its own. `roi memo --check` walks the index and every team page, fails naming the page a failing number came from, rejects a missing or unknown team page, and prints how many pages it checked.

The index states, next to the figures they qualify:

- Registrations: the number of seed registrations so far, or that the seed is not yet registered.
- Return caveats: a legend for the three verdicts (pays off, does not pay off, cannot be told apart from break-even); a flag on a team whose interval rests on one comparison team; a flag on a team whose pre-trend is not testable; `review the rollout` for a negative effect; `no spend` where a window has none; and a glossary of the pre-trend test that says it has low power, so passing it is weak evidence.
- Allocation: the share of each team's increment that lies above the highest monthly spend seen, the cap at 50% beyond it, and, for a team whose curve is not identified, `curve not identified; experiment before budget`.
- Agenda: a note column on each designed experiment, the within-user correlation as a configuration assumption, and any configured spread override printed the same way.
- Unassigned spend: the dollars and the share of company spend that has no owning team, with a banner when that share is above 5%.
- Calibration: each planted effect's source, linked to its publisher or DOI. A public statement is quoted only from a row of `config/citations.yaml` that carries a verification date; a source with no such row is named, with no link and no quote. This README follows the same rule, so any text it puts in double quotes must be such a quote.

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

## How the curve is identified

Each team's dose-response curve is identified from the team's own spend ramp alone, so in this data a learning curve cannot be told from diminishing returns: a team that gets better at using the tools and a team whose tools pay less per dollar at higher spend trace the same line.

The allocation is therefore bounded extrapolation: it never funds a team more than 50% beyond the highest monthly spend observed for that team, and the memo prints the share of each increment above that highest observed spend. The memo also prints the number of seed registrations, so a reader can see how many times the seed was re-registered.

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
| `roi build [--toy] [--config <file>] [--through <month>]` | Load the books, run the dbt models and tests, estimate, compute returns, curves, allocation, agenda and provenance, and publish the warehouse only when every step passed. `--toy` builds the 3-team, 6-month toy company; `--config` generates the books from a generator config first; `--through` replays the incoming months after the books |
| `roi verify --through <month>` | Ledger tie, interval coverage against planted effects, unmeasured discipline |
| `roi memo --month <month> [--check] [--require-signoff]` | Build the static site (the index and one page per team) and, with `--check`, re-run every number's predicate on every page and compare its row count and checksum. `--require-signoff` also requires a sign-off commit signed by a hardware key, needs `.git` and implies `--check` |
| `roi explain <id>` | Print the provenance record behind one number (the id in its `data-prov` attribute) and re-check its rows |
| `roi signoff --month <month>` | A person records that they reviewed the month's memo. It refuses a non-interactive run |
| `roi doctor [--tree \| --release]` | Health check: lock file, books, warehouse, dbt tests, ledger tie, truth boundary. `--release` adds the checks that need `.git` |
| `roi allocate --budget <usd> [--through <month>]` | Print the allocation of a monthly increment in whole dollars across the measured teams without writing anything |
| `roi agenda [--through <month>]`, `roi estimate [--through <month>]` | Print the testing agenda and the team effects without writing anything |
| `roi rollforward --month <month>` | Add a month, re-verify, and swap the warehouse file |
| `roi dbt <args>` | Run a dbt subcommand (for example `roi dbt test`) against the published warehouse, with dbt's output under the work directory |

Every command except `roi dbt` takes `--json` and prints JSON instead of text. `roi allocate` and `roi agenda` also take `--check`, which compares against a committed fixture and is for the test suite.

`--work-dir <dir>` goes before the command (`uv run roi --work-dir <dir> memo ...`), and every command except `roi allocate`, `roi agenda` and `roi estimate` also accepts it after the command name. Commands exit 0 when their checks pass, 1 when one fails, 2 for a usage error, 3 for a data integrity failure and 4 for a fail-closed configuration error.

## Tests

```
uv run pytest -q -m "not slow"
scripts/definition-of-done.sh
```

The first line runs the fast tests; the script adds shell syntax, the scrub check, a toy build with `roi dbt test`, and the tests marked slow (the coverage simulation, the timing test, the kill tests and the side-by-side builds). What the suite proves, by file:

- `tests/test_workflows.py`: the CI workflow runs the tests, then builds the synthetic company with `roi gen`, `roi build`, `roi memo --month 2026-03 --check` and `roi doctor --tree`, inside a network namespace where the runner allows one, so a hidden network call fails. The test pins the triggers, the frozen installs and the absence of `--toy` and secrets.
- `tests/test_concurrent_builds.py`: two `roi build` runs at once, for the toy company and for the synthetic company on separate work dirs, finish with the same warehouse content hash and a byte-identical memo index, and write nothing into the repository.
- `tests/test_benchmark_e2e.py`: the committed Ramp AI Index figures are followed from the reference file through the warehouse to a number on the memo that `roi memo --check` and `roi explain` resolve; a changed value in a copy of the warehouse fails the check with a checksum mismatch.
- `tests/test_estimator_mutation.py`: three one-line edits to a copy of the estimator (a doubled team-effect weight, a covariance divided by n instead of n - 1, a control dropped from a mean) each make `tests/test_estimator.py` fail.
- `tests/test_perf_smoke.py`: the pipeline is timed step by step against a budget for each step, and a step that takes twice its budget fails.
- `tests/test_scenario_08_tie_break.py`: a one-cent edit to the ledger makes `roi verify` and `roi memo --check` exit 3 naming the team and month, and leaves `site/` byte-identical.
- `tests/test_memo_team_pages.py`: there is one team page per team, every number on it is tagged and resolves, and `roi memo --check` refuses a missing or unknown team page.
- `tests/test_citations.py`: every calibration source has a verified citation row, and every quoted string in the memo and in this README is the quote of a verified row.
- `tests/test_readme.py`: this README states the method's limits and the extrapolation bound, and names only commands that exist.

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
