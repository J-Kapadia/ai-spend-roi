-- Estimator input panel, one row per team and month. y_log = direction * ln(outcome), NULL where
-- the outcome is not positive. Months with no outcome (no headcount) are left out of the panel.
-- The rollout month is the first month with AI spend, derived from the books.
with panel as (
    select
        o.team_id,
        o.month,
        o.outcome,
        o.headcount,
        t.direction,
        coalesce(s.spend_cents, 0)::bigint as spend_cents
    from {{ ref('fct_output_team_month') }} as o
    join {{ ref('stg_team') }} as t on t.team_id = o.team_id
    left join {{ ref('fct_ai_spend_team_month') }} as s on s.team_id = o.team_id and s.month = o.month
    where o.outcome is not null
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
