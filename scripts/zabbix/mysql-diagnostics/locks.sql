SELECT /*+ MAX_EXECUTION_TIME(1500) */
 w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx_id,
 w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx_id,
 w.REQUESTING_THREAD_ID AS waiting_thread_id,w.BLOCKING_THREAD_ID AS blocking_thread_id,
 r.OBJECT_SCHEMA,r.OBJECT_NAME,r.INDEX_NAME,
 r.LOCK_MODE AS requested_lock_mode,r.LOCK_TYPE AS requested_lock_type,
 b.LOCK_MODE AS blocking_lock_mode,b.LOCK_TYPE AS blocking_lock_type
FROM performance_schema.data_lock_waits w
LEFT JOIN performance_schema.data_locks r
 ON r.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID AND r.ENGINE=w.ENGINE
LEFT JOIN performance_schema.data_locks b
 ON b.ENGINE_LOCK_ID=w.BLOCKING_ENGINE_LOCK_ID AND b.ENGINE=w.ENGINE
LIMIT 51
