-- Migration 003_export_integrity.sql
-- Persist content identity and byte size for managed report exports.
ALTER TABLE export_inventory ADD COLUMN sha256_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE export_inventory ADD COLUMN byte_size INTEGER NOT NULL DEFAULT 0 CHECK (byte_size >= 0);
