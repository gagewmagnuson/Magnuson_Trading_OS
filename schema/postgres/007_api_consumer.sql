-- 007_api_consumer.sql
-- API-key consumer authentication for the V1 serving API (DEC-021).
--
-- One row per consumer of the read API. The raw API key is NEVER stored; only
-- its SHA-256 hex hash is kept (key_hash), so a database leak cannot reveal a
-- usable key. A key is minted by an admin CLI that prints the raw key once and
-- persists only the hash.
--
-- This is SIMPLE authentication (hash -> lookup -> is_active), NOT multi-tenant
-- authorization: no roles, scopes, or entitlements (ROADMAP: "simple, not
-- multi-tenant"; multi-tenant is V3+). The serving API is read-only; minting and
-- revoking keys is an admin CLI action, not an API operation.
--
-- Reference/config table, intentionally NOT append-only: revoking a key sets
-- is_active=false (never a delete), so it is deliberately excluded from the
-- no-mutation triggers in 001_v0.sql.
--
-- Idempotent: re-running creates nothing new. Assumes 001_v0.sql (meta schema) ran.
create table if not exists meta.api_consumer (
    consumer_id   bigint      generated always as identity primary key,
    label         text        not null,               -- human-readable consumer name, e.g. 'paper-model-v1'
    key_hash      text        not null unique,         -- SHA-256 hex of the API key; the raw key is never stored
    key_prefix    text        not null,               -- non-secret leading chars, to identify a key in logs/UI
    is_active     boolean     not null default true,   -- revoke by setting false; the row is never deleted
    created_at    timestamptz not null default now(),
    revoked_at    timestamptz                          -- set when is_active flips to false; audit trail only
);

comment on table meta.api_consumer is
  'Per-consumer API keys for the V1 read API (DEC-021). Stores only the SHA-256 hash of each key. Simple auth (hash->lookup->is_active), not multi-tenant. Mutable reference data: revoke via is_active=false, never delete.';