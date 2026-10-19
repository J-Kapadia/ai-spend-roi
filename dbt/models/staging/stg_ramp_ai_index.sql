select
    metric || '/' || period as key,
    metric,
    period,
    cast(period || '-01' as date) as month,
    value,
    unit,
    source_url,
    retrieved_on,
    notes
from {{ source('raw', 'ref_ramp_ai_index') }}
