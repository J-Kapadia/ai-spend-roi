select
    employee_id,
    team_id,
    start_date,
    end_date,
    monthly_payroll_cents
from {{ source('raw', 'raw_hr_roster') }}
