{{ config(materialized='table') }}

select
    o.order_id,
    o.customer_id,
    c.name        as customer_name,
    c.email       as customer_email,
    o.amount,
    o.status,
    o.created_at  as ordered_at
from {{ ref('stg_orders') }} o
left join {{ ref('stg_customers') }} c using (customer_id)
