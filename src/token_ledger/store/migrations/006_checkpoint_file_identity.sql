-- Historical checkpoint identity is unknown. Never backfill it from a current path.
-- The runner holds these checkpoints until authenticated historical identity is supplied.
ALTER TABLE source_checkpoints ADD COLUMN file_device INTEGER;
ALTER TABLE source_checkpoints ADD COLUMN file_inode INTEGER;
