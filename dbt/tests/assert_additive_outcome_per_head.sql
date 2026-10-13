-- kind = additive: the outcome is output per employee-month.
select o.team_id, o.month, o.outcome, o.output_signed, o.headcount
from {{ ref('fct_output_team_month') }} as o
join {{ ref('stg_team') }} as t on t.team_id = o.team_id
where t.kind = 'additive'
  and (o.headcount <= 0 or o.outcome is null
       or abs(o.outcome - abs(o.output_signed) / o.headcount) > 1e-9 * greatest(1, abs(o.outcome)))
