-- One row per spend export line (day x user x project x provider x model), with its month.
select
    usage_date,
    date_trunc('month', usage_date)::date as month,
    team_id,
    project_id,
    user_id,
    provider,
    model,
    input_tokens,
    output_tokens,
    cost_cents
from {{ source('raw', 'raw_ai_spend') }}
