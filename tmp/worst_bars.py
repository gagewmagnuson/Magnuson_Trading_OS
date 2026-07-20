"""Throwaway: show the specific bars with the largest high/low/close divergence."""
import duckdb
from trading_os.connectors.tiingo.config import TiingoConfig

cfg = TiingoConfig()
live = f"{(cfg.lake_root/'silver'/'bars_eod').as_posix()}/*.parquet"
stg  = f"{(cfg.lake_root/'silver'/'bars_eod_staging').as_posix()}/*.parquet"

con = duckdb.connect(); con.execute("SET TimeZone='UTC'")
con.execute(f"""
  create view j as
  select o.security_id sid, o.session_date sd,
         o.open o_o,o.high o_h,o.low o_l,o.close o_c,o.volume o_v,
         n.open n_o,n.high n_h,n.low n_l,n.close n_c,n.volume n_v
  from (select security_id,session_date,open,high,low,close,volume,
               row_number() over (partition by security_id,session_date order by knowledge_time desc) rn
        from read_parquet('{live}')) o
  join read_parquet('{stg}') n using (security_id, session_date)
  where o.rn=1
""")
rows = con.execute("""
  select sid, sd,
         o_o,n_o, o_h,n_h, o_l,n_l, o_c,n_c,
         greatest(abs(o_h-n_h),abs(o_l-n_l),abs(o_c-n_c)) as worst
  from j
  where greatest(abs(o_h-n_h),abs(o_l-n_l),abs(o_c-n_c)) >= 0.50
  order by worst desc limit 8
""").fetchall()
print(f"{'sid':>4} {'date':11}  {'O alp->tii':>16} {'H alp->tii':>16} {'L alp->tii':>16} {'C alp->tii':>16}")
for r in rows:
    sid,sd,oo,no,oh,nh,ol,nl,oc,nc,_ = r
    print(f"{sid:>4} {str(sd):11}  {oo:7.2f}->{no:<7.2f} {oh:7.2f}->{nh:<7.2f} {ol:7.2f}->{nl:<7.2f} {oc:7.2f}->{nc:<7.2f}")
con.close()