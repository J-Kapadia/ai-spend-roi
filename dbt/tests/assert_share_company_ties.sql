-- The company row of the payroll share carries exactly the month's total spend and payroll.
select c.month, c.spend_cents, s.spend_cents as total_spend_cents, c.payroll_cents, r.payroll_cents as total_payroll_cents
from {{ ref('fct_ai_share_of_payroll') }} as c
join (select month, sum(spend_cents) as spend_cents from {{ ref('fct_ai_spend_team_month') }} group by 1) as s
  on s.month = c.month
join (select month, sum(payroll_cents) as payroll_cents from {{ ref('int_roster_team_month') }} group by 1) as r
  on r.month = c.month
where c.team_id is null
  and (c.spend_cents <> s.spend_cents or c.payroll_cents <> r.payroll_cents)
