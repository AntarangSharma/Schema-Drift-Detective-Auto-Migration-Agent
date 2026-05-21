{{ config(materialized='view') }}

select
    order_id,
    customer_id,
    amount,
    status,
    created_at
from {{ source('source_raw', 'orders') }}
