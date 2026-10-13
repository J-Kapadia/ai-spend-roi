-- Employees on the roster in a month: start_date on or before the month end, and no end_date
-- or an end_date after the month end. Headcount and payroll are both counted on this basis.
select
    r.team_id,
    m.month,
    count(*)::int as headcount,
    sum(r.monthly_payroll_cents)::bigint as payroll_cents
from {{ ref('stg_hr_roster') }} as r
cross join {{ ref('int_months') }} as m
where r.start_date <= last_day(m.month)
  and (r.end_date is null or r.end_date > last_day(m.month))
group by 1, 2
