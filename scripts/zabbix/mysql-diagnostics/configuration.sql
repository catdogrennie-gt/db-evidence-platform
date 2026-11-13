SELECT /*+ MAX_EXECUTION_TIME(1500) */ VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_variables
WHERE VARIABLE_NAME IN (
 'max_connections','max_user_connections','thread_cache_size','table_open_cache',
 'innodb_buffer_pool_size','innodb_redo_log_capacity','innodb_flush_log_at_trx_commit',
 'sync_binlog','innodb_io_capacity','innodb_io_capacity_max','innodb_flush_method',
 'tmp_table_size','max_heap_table_size','temptable_max_ram','sort_buffer_size',
 'join_buffer_size','read_buffer_size','read_rnd_buffer_size','max_execution_time',
 'innodb_lock_wait_timeout','lock_wait_timeout','wait_timeout','interactive_timeout',
 'slow_query_log','long_query_time','log_output','log_queries_not_using_indexes',
 'min_examined_row_limit','performance_schema','transaction_isolation','read_only',
 'super_read_only','replica_parallel_workers','binlog_format','binlog_expire_logs_seconds',
 'character_set_server','collation_server','max_allowed_packet')
ORDER BY VARIABLE_NAME LIMIT 50
