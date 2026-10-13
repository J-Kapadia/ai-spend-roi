-- Reported output per team and month, signed so that higher is always better. `outcome` is the
-- positive level the estimator works on: output per employee for an `additive` metric, the raw
-- monthly value for a `team_level` metric. It is NULL where an additive team has no headcount.
select
    o.team_id,
    o.month,
    t.direction * o.value as output_signed,
    coalesce(h.headcount, 0)::int as headcount,
    case
        when t.kind = 'team_level' then o.value
        when coalesce(h.headcount, 0) > 0 then o.value / h.headcount
    end as outcome
from {{ ref('stg_output') }} as o
join {{ ref('stg_team') }} as t on t.team_id = o.team_id
left join {{ ref('int_roster_team_month') }} as h on h.team_id = o.team_id and h.month = o.month
