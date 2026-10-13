-- kind = team_level: the outcome is the raw monthly value, not divided by headcount.
select o.team_id, o.month, o.outcome, o.output_signed
from {{ ref('fct_output_team_month') }} as o
join {{ ref('stg_team') }} as t on t.team_id = o.team_id
where t.kind = 'team_level'
  and (o.outcome is null or abs(o.outcome - abs(o.output_signed)) > 1e-9 * greatest(1, abs(o.outcome)))
