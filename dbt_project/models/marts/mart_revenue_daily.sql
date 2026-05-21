{{ config(materialized='table') }}

select
    date_trunc('day', ordered_at)::date as order_date,
    count(*)                            as orders,
    sum(amount)                         as gross_revenue,
    count(distinct customer_id)         as unique_customers
from {{ ref('fct_orders') }}
where status = 'paid'
group by 1
