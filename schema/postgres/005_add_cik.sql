-- 005_add_cik.sql
-- Adds CIK to sec.security as a first-class column (see DEC-017, recorded in
-- chunk 2). CIK is the SEC's Central Index Key: an ISSUER-level, immutable
-- identifier, so it is NOT effective-dated and does not belong in
-- sec.security_identifier (which models identifiers that change over time).
-- Nullable: set when resolvable from company_tickers.json; share classes of one
-- issuer share a CIK (mild, harmless denormalization).
alter table sec.security add column if not exists cik text;

comment on column sec.security.cik is
  'SEC Central Index Key (issuer-level, immutable). Nullable; set by the universe layer when resolvable. Share classes of one issuer share a CIK.';

create index if not exists ix_security_cik on sec.security (cik);