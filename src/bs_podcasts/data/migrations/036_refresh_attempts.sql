ALTER TABLE shows ADD COLUMN last_refresh_attempt REAL;
UPDATE shows SET last_refresh_attempt=last_refresh WHERE last_refresh IS NOT NULL;
