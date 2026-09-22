UPDATE shows SET fail_count=0, suspended=0, health='unknown'
WHERE source='local' AND (fail_count>0 OR suspended=1);
