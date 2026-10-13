-- One output row per team and month.
select team_id, month, count(*) as n
from {{ ref('stg_output') }}
group by 1, 2
having count(*) > 1
