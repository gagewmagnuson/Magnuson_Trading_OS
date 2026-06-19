"""
DuckDB analytical engine (V0 capstone).

The compute layer from SCHEMA.md §4/§5 and DEC-003: DuckDB reads the Parquet
lake in place and ATTACHes PostgreSQL read-only, so a single query is
point-in-time across BOTH stores with no data duplication. Postgres is the
system of record; DuckDB owns no data.

This package houses the V0 cross-store PIT proof. It will later grow into the
broader execution / feature-generation / research-query layer — hence "engine".
"""