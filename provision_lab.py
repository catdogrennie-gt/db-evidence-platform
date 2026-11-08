"""Provision fresh dedicated directories only; refuses an existing instance.

Requires an installed official MySQL binary and preverified SSH known_hosts.
Credentials come exclusively from ignored runtime/config.json.
"""
import json,secrets
import lab


def main():
    cfg=lab.config()
    if cfg['source_port']!=3307 or cfg['restore_port']!=3308:
        raise ValueError('only dedicated lab ports 3307/3308 are supported')
    with lab.ssh() as c:
        lab.remote(c,'set -eu; test -x /opt/tdx-lab/mysql/bin/mysqld; id mysql; test ! -e /opt/tdx-lab/3307; test ! -e /opt/tdx-lab/3308')
        # Restrict SQL password characters instead of interpolating external text.
        pw=secrets.token_hex(20)
        cfg['db_password']=pw
        (lab.RUNTIME/'config.json').write_text(json.dumps(cfg,indent=2),encoding='utf-8')
        s=c.open_sftp()
        try:
            for port in [3307,3308]:
                path=f'/opt/tdx-lab/{port}'
                lab.remote(c,f'mkdir -p {path}/data; chown -R mysql:mysql {path}')
                cnf=f'''[mysqld]
user=mysql
basedir=/opt/tdx-lab/mysql
datadir={path}/data
port={port}
bind-address=127.0.0.1
socket={path}/mysql.sock
pid-file={path}/mysql.pid
log-error={path}/error.log
server-id={port}
mysqlx=OFF
performance_schema=ON
slow_query_log=ON
long_query_time=0.05
slow_query_log_file={path}/slow.log
innodb_buffer_pool_size=256M
'''
                with s.file(path+'/my.cnf','w') as f:f.write(cnf)
                lab.remote(c,f'/opt/tdx-lab/mysql/bin/mysqld --no-defaults --initialize-insecure --user=mysql --basedir=/opt/tdx-lab/mysql --datadir={path}/data',timeout=120)
                lab.remote(c,f'/opt/tdx-lab/mysql/bin/mysqld --defaults-file={path}/my.cnf --daemonize')
                sql=f'''ALTER USER 'root'@'localhost' IDENTIFIED BY '{pw}';
CREATE USER 'lab_operator'@'127.0.0.1' IDENTIFIED BY '{pw}';
GRANT ALL PRIVILEGES ON evidence_lab.* TO 'lab_operator'@'127.0.0.1';
CREATE USER 'lab_reader'@'127.0.0.1' IDENTIFIED BY '{pw}';
GRANT SELECT, PROCESS, REPLICATION CLIENT ON *.* TO 'lab_reader'@'127.0.0.1';
CREATE DATABASE evidence_lab;
'''
                # Create with owner-only permissions before writing secrets.
                lab.remote(c,f'install -m 600 /dev/null {path}/setup.sql; install -m 600 /dev/null {path}/client.cnf')
                with s.file(path+'/setup.sql','w') as f:f.write(sql)
                try:
                    lab.remote(c,f'/opt/tdx-lab/mysql/bin/mysql --no-defaults -S {path}/mysql.sock -u root < {path}/setup.sql')
                finally:s.remove(path+'/setup.sql')
                with s.file(path+'/client.cnf','w') as f:f.write(f'[client]\nuser=root\npassword={pw}\nsocket={path}/mysql.sock\n')
                with s.file(path+'/data/auto.cnf') as f:
                    text=f.read().decode()
                server_uuid=next(line.split('=',1)[1].strip() for line in text.splitlines() if line.startswith('server-uuid='))
                cfg[('source' if port==3307 else 'restore')+'_uuid']=server_uuid
                (lab.RUNTIME/'config.json').write_text(json.dumps(cfg,indent=2),encoding='utf-8')
        finally:s.close()
    lab.seed()
    print('Fresh lab initialized, UUIDs registered and synthetic data seeded.')


if __name__=='__main__':main()
