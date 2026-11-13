SELECT /*+ MAX_EXECUTION_TIME(1500) */ UNIX_TIMESTAMP(start_time) AS event_epoch,start_time,
 TIME_TO_SEC(query_time) AS query_time_seconds,
 TIME_TO_SEC(lock_time) AS lock_time_seconds,rows_sent,rows_examined,
 thread_id,server_id
FROM mysql.slow_log
WHERE start_time>=NOW(6)-INTERVAL 10 MINUTE
ORDER BY start_time DESC LIMIT 101
