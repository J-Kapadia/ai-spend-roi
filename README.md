# ai-spend-roi

Which teams' AI spend pays off, and where the next dollar of AI budget should go

A finance method that ties a company's AI spend, by team, to what each team produces. It reports a return only where a fair comparison group exists, says unmeasured and designs the experiment everywhere else, and allocates a stated budget to where measured marginal return is highest. It runs on synthetic data for a made-up company (Beta Co) and re-runs every month. The output is a static memo site in which every number links back to the source rows that produced it.

Status: work in progress. The commands below are the product's interface.

## What it answers

1. Does our AI spend pay off? Per team, with intervals, or unmeasured with the reason.
2. Where does the next dollar go? An allocation of a stated budget across the measured teams.
3. What should we test next? A designed experiment for each unmeasured team.

## What the result does and does not show

The data is synthetic. The generator plants a known effect of AI in each team, calibrated to published research, and keeps the answer in a file that the method never reads. Recovering the planted effects shows that the method works. It is not a finding about any real company.

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

The memo is written to `site/`. `uv run roi explain <number>` prints the provenance record behind any number in it. Money is kept in integer cents, and the same seed and commit give an identical warehouse and byte-identical memo HTML.

## Tests

```
uv run pytest -q
```

## Verifying the pre-registration and sign-offs

The seed, the planted effects and the thresholds are fixed before the estimator is run, and their hash is in `config/prereg.sha256`. Each registration is a signed tag `prereg-v<n>`, and each monthly sign-off is a signed tag `signoff-<month>`. To check one:

```
git show <tag>:config/allowed-signers > signers
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=signers tag -v <tag>
git show prereg-v<n>:config/prereg.sha256
```

The last command prints the hash that `config/prereg.sha256` must equal at that tag. The signing key is listed in `config/allowed-signers`.
