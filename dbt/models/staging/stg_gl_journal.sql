-- One row per ledger line. `period` (YYYY-MM) becomes the first day of the month; net is
-- debit minus credit; `is_ai_usage` marks the AI-usage account named in the build settings.
select
    entry_id,
    cast(period || '-01' as date) as month,
    account_code,
    cost_center as team_id,
    debit_cents,
    credit_cents,
    debit_cents - credit_cents as net_cents,
    account_code = (select value from {{ source('raw', 'roi_settings') }} where key = 'gl_account') as is_ai_usage,
    memo
from {{ source('raw', 'raw_gl_journal') }}
