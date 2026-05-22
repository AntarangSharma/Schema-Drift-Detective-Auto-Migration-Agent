"""BI adapters — map dbt models to downstream BI assets."""

from schema_drift.bi.metabase import MetabaseAdapter, MetabaseConfig

__all__ = ["MetabaseAdapter", "MetabaseConfig"]
