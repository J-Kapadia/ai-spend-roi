select team_id, month, count(*) as n
from {{ ref('fct_ai_share_of_payroll') }}
group by team_id, month
having count(*) > 1
