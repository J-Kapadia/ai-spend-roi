-- Every month a team reports output, it has people on the roster. Without them an additive
-- metric has no per-head level, so the build stops rather than drop the month.
select team_id, month, headcount
from {{ ref('fct_output_team_month') }}
where headcount <= 0
