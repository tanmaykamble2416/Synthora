-- Runs once, when the Postgres data volume is first initialised.
-- The authoritative way to enable extensions is the first Alembic migration (Day 2);
-- this script exists so the Day 1 health check can verify pgvector immediately.
CREATE EXTENSION IF NOT EXISTS vector;
