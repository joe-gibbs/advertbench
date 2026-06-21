DROP INDEX IF EXISTS idx_models_active_slug;

ALTER TABLE models
  DROP COLUMN IF EXISTS provider,
  DROP COLUMN IF EXISTS active;
