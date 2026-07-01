"""
Tiingo client.

Currently used as the validation oracle for the corporate-action adjustment
engine (Tiingo follows CRSP methodology for adjusted closes — the institutional
reference). A single endpoint returns, per day, the raw OHLCV, the inline
corporate actions (divCash on the ex-date, splitFactor != 1 on a split ex-date),
and Tiingo's own adjClose — so the actions we seed and the reference we validate
against come from one internally-consistent snapshot.

Designed to graduate into the Phase-3 corporate-actions connector: same client,
same normalized Action shape, writing into corp.corporate_action.
"""