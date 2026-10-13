select team_id, month, count(*) as n
from {{ ref('fct_ai_spend_team_month') }}
group by 1, 2
having count(*) > 1
