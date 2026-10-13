-- direction = 1 keeps the sign, direction = -1 flips it, in the signed output and in y_log.
-- Reported outputs are positive levels, so the signed output has the sign of the direction.
select o.team_id, o.month, t.direction, o.output_signed, p.y_log
from {{ ref('fct_output_team_month') }} as o
join {{ ref('stg_team') }} as t on t.team_id = o.team_id
left join {{ ref('fct_team_month') }} as p on p.team_id = o.team_id and p.month = o.month
where sign(o.output_signed) <> t.direction
   or (p.y_log is not null and abs(p.y_log - t.direction * ln(p.outcome)) > 1e-12)
