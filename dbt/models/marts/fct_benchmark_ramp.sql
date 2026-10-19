-- Per company month and index metric, the published figure that applies (the latest index period at or
-- before the month) beside the company's own share of teams with AI spend. Months before the index's
-- first period have no row.
with company as (
    select
        month,
        count(distinct team_id)::integer as teams,
        count(distinct case when rollout_month <= month then team_id end)::integer as teams_with_ai
    from {{ ref('fct_team_month') }}
    group by month
),

applicable as (
    select
        m.month,
        i.metric,
        max(i.month) as index_month
    from {{ ref('int_months') }} as m
    join {{ ref('stg_ramp_ai_index') }} as i on i.month <= m.month
    group by m.month, i.metric
)

select
    a.month::date as month,
    a.metric::varchar as metric,
    cur.period::varchar as index_period,
    cur.value::decimal(18, 4) as index_value,
    cur.unit::varchar as index_unit,
    prior.period::varchar as prior_period,
    prior.value::decimal(18, 4) as prior_value,
    cur.source_url::varchar as source_url,
    cur.retrieved_on::date as retrieved_on,
    coalesce(c.teams, 0)::integer as teams,
    coalesce(c.teams_with_ai, 0)::integer as teams_with_ai,
    (case when coalesce(c.teams, 0) = 0 then 0 else c.teams_with_ai::decimal(18, 9) / c.teams end)::decimal(18, 9)
        as company_share
from applicable as a
join {{ ref('stg_ramp_ai_index') }} as cur on cur.metric = a.metric and cur.month = a.index_month
left join {{ ref('stg_ramp_ai_index') }} as prior
    on prior.metric = cur.metric and prior.month = cur.month - interval 12 month
left join company as c on c.month = a.month
