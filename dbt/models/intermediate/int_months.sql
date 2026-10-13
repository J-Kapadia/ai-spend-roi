-- The months the books report output for: the spine of every team-month panel.
select distinct month from {{ ref('stg_output') }}
