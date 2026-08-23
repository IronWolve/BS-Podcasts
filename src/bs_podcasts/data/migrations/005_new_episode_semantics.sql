-- Historical backlog imported during the early build was incorrectly marked
-- as newly arrived. Future imports set this flag based on whether a show has
-- already completed its initial episode import.
UPDATE episodes SET is_new=0;
