-- episodes_show_published (migration 001) is a leading-column prefix of
-- episodes_show_latest (migration 021). Every episode write maintained it
-- for no query the longer index does not already serve.
DROP INDEX IF EXISTS episodes_show_published;
