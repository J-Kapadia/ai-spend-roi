select team_id, month, share
from {{ ref('fct_ai_share_of_payroll') }}
where share < 0
