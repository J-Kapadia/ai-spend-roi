-- Estimator input panel, one row per team and month (plan §7.1, D-05).
-- Walking-skeleton version: spend, headcount, outcome per head, y_log, rollout month.
with months as (
    select distinct month from {{ source('raw', 'raw_output') }}
),

spend as (
    select
        team_id,
        date_trunc('month', usage_date)::date as month,
        sum(cost_cents)::bigint as spend_cents
    from {{ source('raw', 'raw_ai_spend') }}
    group by 1, 2
),

headcount as (
    select
        r.team_id,
        m.month,
        count(*)::int as headcount
    from {{ source('raw', 'raw_hr_roster') }} as r
    cross join months as m
    where r.start_date <= last_day(m.month)
      and (r.end_date is null or r.end_date > last_day(m.month))
    group by 1, 2
),

panel as (
    select
        o.team_id,
        o.month,
        o.value / h.headcount as outcome,
        coalesce(s.spend_cents, 0)::bigint as spend_cents,
        h.headcount,
        t.direction
    from {{ source('raw', 'raw_output') }} as o
    join headcount as h on h.team_id = o.team_id and h.month = o.month
    join {{ source('raw', 'dim_team') }} as t on t.team_id = o.team_id
    left join spend as s on s.team_id = o.team_id and s.month = o.month
)

select
    team_id,
    month,
    outcome,
    case when outcome > 0 then direction * ln(outcome) end as y_log,
    spend_cents,
    spend_cents / headcount as spend_per_head_cents,
    headcount,
    min(case when spend_cents > 0 then month end) over (partition by team_id) as rollout_month
from panel
