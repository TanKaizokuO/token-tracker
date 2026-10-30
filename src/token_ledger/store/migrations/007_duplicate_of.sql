-- Same-call duplicates (same harness, provider and provider_request_id, differing only by
-- account alias) point at the canonical row that carries their usage. Managed by
-- token_ledger.ingest.overlap.recompute_overlap_flags; NULL means "not a duplicate".
ALTER TABLE canonical_requests ADD COLUMN duplicate_of TEXT REFERENCES canonical_requests(canonical_id) ON DELETE SET NULL;
