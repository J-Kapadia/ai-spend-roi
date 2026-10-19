# Reference figures

`ramp_ai_index.csv` holds figures published by the Ramp AI Index
(https://ramp.com/data/ai-index), transcribed by hand so the memo can set the
company beside a public benchmark. The code never fetches anything: this file is
the only source, and the pipeline and tests run with no network.

## Columns

| column | meaning |
| --- | --- |
| `metric` | lower-case id matching `[a-z0-9_]+`; `paid_adoption_share` is the index headline: the share of US businesses paying for AI tools in the month |
| `period` | the month the figure is for, `YYYY-MM` |
| `value` | the figure as printed, a decimal with at most four places; a percentage is the printed percent number (44.4, not 0.444) |
| `unit` | one of `percent`, `usd`, `count` |
| `source_url` | the public Ramp page the figure was read from, or an Internet Archive capture of that page |
| `retrieved_on` | the ISO date the figure was read |
| `notes` | what the figure covers in the page's own terms (population, how the month is defined) and, for an archived capture, its capture time |

Rows are unique on (`metric`, `period`) and sorted by metric, then period.

## Selection rule

Only figures the pages state as numbers are transcribed, for every month they
state. Archive captures of a page are allowed. Nothing is estimated from a chart
and no row is invented. The `paid_adoption_share` values are the numbers in the
data the page's adoption chart is drawn from; no value is read off the picture.
The page revises its history, so `retrieved_on` says which reading a row is.

## Use in the memo

The figures are loaded verbatim into `ref_ramp_ai_index` by `roi build`, compared
in `fct_benchmark_ramp`, and printed in the memo's benchmark section with the
source page and retrieval date beside them.
