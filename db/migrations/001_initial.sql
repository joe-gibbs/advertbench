CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS models (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE,
  display_name text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS generation_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  prompt text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  requested_sizes jsonb NOT NULL,
  config_snapshot jsonb NOT NULL,
  generated_with text NOT NULL DEFAULT 'mock',
  error text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS output_sets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
  model_id uuid NOT NULL REFERENCES models(id) ON DELETE RESTRICT,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  prompt text NOT NULL,
  rating integer NOT NULL DEFAULT 1200,
  matches integer NOT NULL DEFAULT 0,
  wins integer NOT NULL DEFAULT 0,
  losses integer NOT NULL DEFAULT 0,
  generation_ms integer,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (run_id, model_id)
);

CREATE TABLE IF NOT EXISTS ad_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  output_set_id uuid NOT NULL REFERENCES output_sets(id) ON DELETE CASCADE,
  size_key text NOT NULL,
  label text NOT NULL,
  width integer NOT NULL CHECK (width > 0),
  height integer NOT NULL CHECK (height > 0),
  storage_path text NOT NULL,
  public_path text NOT NULL,
  mime_type text NOT NULL,
  checksum text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (output_set_id, size_key)
);

CREATE TABLE IF NOT EXISTS votes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pair_key text NOT NULL,
  winner_set_id uuid NOT NULL REFERENCES output_sets(id) ON DELETE RESTRICT,
  loser_set_id uuid NOT NULL REFERENCES output_sets(id) ON DELETE RESTRICT,
  voter_hash text NOT NULL,
  ip_hash text NOT NULL,
  user_agent_hash text NOT NULL,
  idempotency_key text,
  winner_rating_before integer NOT NULL,
  loser_rating_before integer NOT NULL,
  winner_rating_after integer NOT NULL,
  loser_rating_after integer NOT NULL,
  k_factor integer NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (winner_set_id <> loser_set_id),
  UNIQUE (voter_hash, pair_key),
  UNIQUE (voter_hash, idempotency_key)
);

CREATE TABLE IF NOT EXISTS vote_events (
  id bigserial PRIMARY KEY,
  event_type text NOT NULL,
  voter_hash text NOT NULL,
  ip_hash text NOT NULL,
  user_agent_hash text NOT NULL,
  accepted boolean NOT NULL,
  reason text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_generation_runs_status_created ON generation_runs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_output_sets_completed_rating ON output_sets (rating DESC, completed_at DESC) WHERE status = 'completed';
CREATE INDEX IF NOT EXISTS idx_output_sets_model_completed ON output_sets (model_id, completed_at DESC) WHERE status = 'completed';
CREATE INDEX IF NOT EXISTS idx_output_sets_run_completed ON output_sets (run_id, status, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ad_assets_set_size ON ad_assets (output_set_id, size_key);
CREATE INDEX IF NOT EXISTS idx_votes_winner_created ON votes (winner_set_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_votes_loser_created ON votes (loser_set_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_votes_created ON votes (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vote_events_voter_recent ON vote_events (voter_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vote_events_ip_recent ON vote_events (ip_hash, created_at DESC);
