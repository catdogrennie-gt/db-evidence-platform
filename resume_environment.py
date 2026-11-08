"""Restore the already-provisioned local lab after a VM/host restart."""
import subprocess,time
from pathlib import Path
import lab

def main():
    cfg=lab.config();vmrun=Path('C:/Program Files (x86)/VMware/VMware Workstation/vmrun.exe')
    vm=cfg['vm_path']
    running=subprocess.run([str(vmrun),'list'],capture_output=True,text=True,check=True).stdout.replace('\\','/')
    if vm not in running:subprocess.run([str(vmrun),'start',vm,'nogui'],capture_output=True,check=True)
    # The VM network is isolated VMnet2; restore its secondary host-reachable address.
    args=[str(vmrun),'-gu',cfg['ssh_user'],'-gp',cfg['ssh_password'],'runProgramInGuest',vm,'/bin/sh','-c','ip -4 addr show dev ens35 | grep -q 192.168.2.132/24 || ip addr add 192.168.2.132/24 dev ens35']
    for attempt in range(30):
        result=subprocess.run(args,capture_output=True)
        if result.returncode==0:break
        time.sleep(2)
    else:raise RuntimeError('VM guest tools or configured credentials unavailable')
    # Guest tools may become ready before the network service finishes configuring.
    for attempt in range(20):
        subprocess.run(args,capture_output=True)
        try:
            with lab.ssh() as c:
                for port in [3307,3308]:
                    path=f'/opt/tdx-lab/{port}'
                    cmd=f"if [ -f {path}/mysql.pid ] && kill -0 $(cat {path}/mysql.pid) 2>/dev/null; then echo '{port} already running'; else /opt/tdx-lab/mysql/bin/mysqld --defaults-file={path}/my.cnf --daemonize; fi"
                    lab.remote(c,cmd)
            break
        except (OSError,lab.paramiko.SSHException):time.sleep(2)
    else:raise RuntimeError('VM SSH network did not become ready')
    with lab.connection('operator') as db:lab.query(db,'SELECT 1')
    with lab.connection('operator','restore') as db:lab.query(db,'SELECT 1')
    print('Registered MySQL source and restore instances are ready.')
if __name__=='__main__':main()
