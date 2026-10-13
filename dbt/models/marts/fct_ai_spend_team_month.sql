-- AI spend per team and month beside the ledger's AI-usage line for the same key. The bucket
-- `unassigned` is a key like any other. tie_diff_cents = spend - ledger and must be 0.
with spend as (
    select
        team_id,
        month,
        sum(cost_cents)::bigint as spend_cents,
        sum(input_tokens)::bigint as input_tokens,
        sum(output_tokens)::bigint as output_tokens,
        count(distinct user_id)::int as active_users
    from {{ ref('stg_ai_spend') }}
    group by 1, 2
),

gl as (
    select
        team_id,
        month,
        sum(net_cents)::bigint as gl_cents
    from {{ ref('stg_gl_journal') }}
    where is_ai_usage
    group by 1, 2
)

select
    coalesce(s.team_id, g.team_id) as team_id,
    coalesce(s.month, g.month) as month,
    coalesce(s.spend_cents, 0)::bigint as spend_cents,
    coalesce(s.input_tokens, 0)::bigint as input_tokens,
    coalesce(s.output_tokens, 0)::bigint as output_tokens,
    coalesce(s.active_users, 0)::int as active_users,
    coalesce(g.gl_cents, 0)::bigint as gl_cents,
    (coalesce(s.spend_cents, 0) - coalesce(g.gl_cents, 0))::bigint as tie_diff_cents
from spend as s
full outer join gl as g on s.team_id = g.team_id and s.month = g.month
