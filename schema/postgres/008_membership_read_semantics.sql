-- 008_membership_read_semantics.sql
-- DEC-027: universe membership research reads are EVENT-TIME ONLY.
--
-- members_asof originally gated on knowledge_time <= as_of, which makes every
-- past-dated research query empty for a reconstructed membership record whose
-- honest knowledge_time is load time (2026). For reconstructed index membership
-- there is no meaningful "what had we captured by then" (the system did not exist
-- at the historical dates), so the research read uses event-time only and returns
-- today's best reconstruction. knowledge_time is retained honestly for provenance
-- and exposed for audit via members_asof_bitemporal.
--
-- No data is modified; this redefines one function and adds one.

-- Research read: event-time only. The default, correct for backtests.
create or replace function univ.members_asof(p_universe_code text, p_as_of date)
returns table (security_id bigint) language sql stable as $$
    select m.security_id
    from univ.universe_membership m
    join univ.universe u on u.universe_id = m.universe_id
    where u.code = p_universe_code
      and m.valid_from <= p_as_of
      and (m.valid_to is null or m.valid_to > p_as_of)
$$;

-- Audit read: strict bitemporal. "Who did the DB know was a member, given only
-- membership facts recorded on/before p_known_as_of?" For validation and audit,
-- not routine research (DEC-027).
create or replace function univ.members_asof_bitemporal(
    p_universe_code text, p_as_of date, p_known_as_of timestamptz
)
returns table (security_id bigint) language sql stable as $$
    select m.security_id
    from univ.universe_membership m
    join univ.universe u on u.universe_id = m.universe_id
    where u.code = p_universe_code
      and m.knowledge_time <= p_known_as_of
      and m.valid_from <= p_as_of
      and (m.valid_to is null or m.valid_to > p_as_of)
$$;