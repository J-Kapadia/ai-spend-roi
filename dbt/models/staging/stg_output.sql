select
    team_id,
    month,
    metric_id,
    value
from {{ source('raw', 'raw_output') }}
