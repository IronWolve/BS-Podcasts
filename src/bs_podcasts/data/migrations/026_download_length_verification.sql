-- Historical downloads have no saved framing evidence. Keep them playable,
-- but require verified length before automatic housekeeping may delete them.
ALTER TABLE downloads ADD COLUMN length_verified INTEGER NOT NULL DEFAULT 0;
