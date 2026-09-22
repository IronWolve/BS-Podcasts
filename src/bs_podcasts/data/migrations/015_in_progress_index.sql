-- Continue listening is independent of the recent episode page (audit A11).
CREATE INDEX episodes_in_progress ON episodes(last_played DESC, id DESC)
WHERE played=0 AND position_seconds>0;
