{{ config(materialized='view') }}

select
    customer_id,
    name,
    email,
    created_at
from {{ source('source_raw', 'customers') }}
