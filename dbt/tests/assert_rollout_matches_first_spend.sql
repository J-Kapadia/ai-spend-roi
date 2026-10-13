-- The rollout month is the first month with AI spend; a team without spend has none.
select p.team_id, p.rollout_month, f.first_spend_month
from (select distinct team_id, rollout_month from {{ ref('fct_team_month') }}) as p
left join (
    select team_id, min(month) as first_spend_month
    from {{ ref('fct_ai_spend_team_month') }}
    where spend_cents > 0
    group by 1
) as f on f.team_id = p.team_id
where p.rollout_month is distinct from f.first_spend_month
