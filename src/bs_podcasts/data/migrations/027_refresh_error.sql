-- Preserve the most recent meaningful failure, including after suspension.
-- Rearming a feed retains its cause until a successful refresh clears it.
ALTER TABLE shows ADD COLUMN last_refresh_error TEXT NOT NULL DEFAULT '';
