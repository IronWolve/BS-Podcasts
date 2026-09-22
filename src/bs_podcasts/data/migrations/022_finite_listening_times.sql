-- Invalid timing metadata must not keep breaking an episode's details view.
-- Transcript text is retained; unusable chapters can be fetched again.
DELETE FROM chapters WHERE start_seconds IS NULL OR start_seconds NOT BETWEEN 0 AND 2147483648;
UPDATE chapters SET end_seconds=NULL WHERE end_seconds NOT BETWEEN 0 AND 2147483648 OR end_seconds<start_seconds;
UPDATE transcript_segments SET start_seconds=NULL WHERE start_seconds NOT BETWEEN 0 AND 2147483648;
UPDATE transcript_segments SET end_seconds=NULL WHERE end_seconds NOT BETWEEN 0 AND 2147483648 OR end_seconds<start_seconds;
