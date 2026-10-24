\set ON_ERROR_STOP on
-- No partitions were selected for this release. This file is READ ONLY.
-- There is no missing-partition risk, DEFAULT partition or partition-creation job.
SELECT c.relname,c.reltuples::bigint AS estimated_rows,
 pg_size_pretty(pg_total_relation_size(c.oid)) total_size
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='korsik' AND c.relkind='r' ORDER BY pg_total_relation_size(c.oid) DESC;
SELECT relname,n_live_tup,n_dead_tup,last_autovacuum,last_autoanalyze
FROM pg_stat_user_tables WHERE schemaname='korsik' ORDER BY n_live_tup DESC;
-- Empty by design. Any row means a new migration changed the partition strategy.
SELECT p.relname parent,c.relname partition FROM pg_inherits i
JOIN pg_class p ON p.oid=i.inhparent JOIN pg_class c ON c.oid=i.inhrelid
JOIN pg_namespace n ON n.oid=p.relnamespace WHERE n.nspname='korsik';
