-- Company-month totals tie to the cent too.
select month, sum(spend_cents) as spend_cents, sum(gl_cents) as gl_cents
from {{ ref('fct_ai_spend_team_month') }}
group by month
having sum(spend_cents) <> sum(gl_cents)
