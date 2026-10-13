-- Spend ties to the ledger to the cent for every team-month, including `unassigned`.
select team_id, month, spend_cents, gl_cents, tie_diff_cents
from {{ ref('fct_ai_spend_team_month') }}
where tie_diff_cents <> 0
