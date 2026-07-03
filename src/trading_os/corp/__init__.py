"""
Corporate-action primitives shared across ingestion sources.

Vendor-neutral: any connector (Tiingo now; Polygon/SEC/manual later) writes
corporate actions through the single canonical primitive in action_write.py, so
there is exactly one implementation of the append-only, payload-aware,
conflict-detecting write — forever. No connector mutates or invalidates another
connector's rows (DEC-019); sources coexist and are resolved at read time.
"""