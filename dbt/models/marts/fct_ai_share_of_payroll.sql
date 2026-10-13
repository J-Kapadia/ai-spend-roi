-- AI spend as a share of payroll, by team and for the company (team_id NULL), by month. The
-- company row carries all spend, including the `unassigned` bucket, over all payroll.
with team_rows as (
    select
        t.team_id,
        m.month,
        coalesce(s.spend_cents, 0)::bigint as spend_cents,
        coalesce(r.payroll_cents, 0)::bigint as payroll_cents
    from {{ ref('stg_team') }} as t
    cross join {{ ref('int_months') }} as m
    left join {{ ref('fct_ai_spend_team_month') }} as s on s.team_id = t.team_id and s.month = m.month
    left join {{ ref('int_roster_team_month') }} as r on r.team_id = t.team_id and r.month = m.month
),

company_rows as (
    select
        null::varchar as team_id,
        m.month,
        coalesce((select sum(s.spend_cents) from {{ ref('fct_ai_spend_team_month') }} as s where s.month = m.month), 0)::bigint as spend_cents,
        coalesce((select sum(r.payroll_cents) from {{ ref('int_roster_team_month') }} as r where r.month = m.month), 0)::bigint as payroll_cents
    from {{ ref('int_months') }} as m
),

all_rows as (
    select * from team_rows
    union all
    select * from company_rows
)

select
    team_id,
    month,
    spend_cents,
    payroll_cents,
    case when payroll_cents > 0 then (spend_cents::decimal(38, 9) / payroll_cents)::decimal(18, 9) end as share
from all_rows
