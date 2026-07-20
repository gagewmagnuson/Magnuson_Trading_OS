"""Throwaway: characterize the stage-E price diffs on the 5-ticker staging vs live."""
import duckdb
from trading_os.connectors.tiingo.config import TiingoConfig

cfg = TiingoConfig()
live = f"{(cfg.lake_root/'silver'/'bars_eod').as_posix()}/*.parquet"
stg  = f"{(cfg.lake_root/'silver'/'bars_eod_staging').as_posix()}/*.parquet"

con = duckdb.connect(); con.execute("SET TimeZone='UTC'")
con.execute(f"""
  create view j as
  select o.security_id, o.session_date,
         o.open o_open, n.open n_open, o.high o_high, n.high n_high,
         o.low o_low, n.low n_low, o.close o_close, n.close n_close
  from (select security_id,session_date,open,high,low,close,
               row_number() over (partition by security_id,session_date order by knowledge_time desc) rn
        from read_parquet('{live}')) o
  join read_parquet('{stg}') n using (security_id, session_date)
  where o.rn = 1
""")
for col in ["open","high","low","close"]:
    r = con.execute(f"""
      select count(*) filter (where abs(o_{col}-n_{col}) > 0.001*greatest(abs(o_{col}),abs(n_{col}))) as n_diff,
             count(*) filter (where abs(o_{col}-n_{col}) > 0.001*greatest(abs(o_{col}),abs(n_{col})) and abs(o_{col}-n_{col}) < 0.10) as n_small,
             count(*) filter (where abs(o_{col}-n_{col}) >= 0.50) as n_big,
             max(abs(o_{col}-n_{col})) as max_abs
      from j
    """).fetchone()
    print(f"{col:6}  diffs={r[0]:4}  <$0.10={r[1]:4}  >=$0.50={r[2]:3}  max_abs=${r[3]:.2f}")
con.close()