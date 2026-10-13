select
    team_id,
    name,
    metric_id,
    unit_label,
    direction,
    kind
from {{ source('raw', 'dim_team') }}
