"""Throwaway: which securities make up stage B's coverage-lost pairs?"""
import duckdb
from trading_os.connectors.tiingo.config import TiingoConfig
cfg = TiingoConfig()
live = f"{(cfg.lake_root/'silver'/'bars_eod').as_posix()}/*.parquet"
stg  = f"{(cfg.lake_root/'silver'/'bars_eod_staging').as_posix()}/*.parquet"
con = duckdb.connect(); con.execute("SET TimeZone='UTC'")
rows = con.execute(f"""
  select o.security_id, o.symbol, count(*) lost
  from (select distinct security_id, symbol, session_date from read_parquet('{live}')) o
  left join (select distinct security_id, session_date from read_parquet('{stg}')) n
    using (security_id, session_date)
  where n.security_id is null
  group by 1,2 order by lost desc
""").fetchall()
print(f"{'sid':>5} {'symbol':8} {'lost_sessions':>14}")
for r in rows: print(f"{r[0]:>5} {r[1]:8} {r[2]:>14}")
print(f"\n{len(rows)} securities with any missing coverage")
con.close()