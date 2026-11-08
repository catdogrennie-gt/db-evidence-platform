"""Fixed Linux read protocol. No profile/model-supplied shell commands."""

import time
from ..connectors import remaining, safe_error_code
from ..metrics_derivation import derive
from .logs import result
from .mysql import BOOT_ID

FILES = (
    "/proc/stat",
    "/proc/meminfo",
    "/proc/loadavg",
    "/proc/uptime",
    "/proc/diskstats",
    "/proc/sys/kernel/random/boot_id",
    "/etc/hostname",
)
DF_COMMAND = "LC_ALL=C df -Pk /"
FILE_LIMIT = 256 * 1024


def parse_host_files(files):
    values = {}
    if "/proc/stat" in files:
        line = next((v for v in files["/proc/stat"].splitlines() if v.startswith("cpu ")), None)
        if line:
            numbers = [
                int(v) for v in line.split()[1:9]
            ]  # guest ticks already included in user/nice
            values["cpu_total_ticks"] = sum(numbers)
            values["cpu_idle_ticks"] = numbers[3] + numbers[4]
    if "/proc/meminfo" in files:
        memory = {
            line.split(":", 1)[0]: int(line.split()[1]) * 1024
            for line in files["/proc/meminfo"].splitlines()
            if ":" in line
        }
        values["memory_total_bytes"] = memory.get("MemTotal")
        values["memory_available_bytes"] = memory.get("MemAvailable")  # never substitute MemFree
    if "/proc/loadavg" in files:
        load = files["/proc/loadavg"].split()
        values.update(load1=float(load[0]), load5=float(load[1]), load15=float(load[2]))
    if "/proc/uptime" in files:
        values["uptime_seconds"] = float(files["/proc/uptime"].split()[0])
    if "/proc/sys/kernel/random/boot_id" in files:
        values["host_boot_id"] = files["/proc/sys/kernel/random/boot_id"].strip()
    if "/etc/hostname" in files:
        values["hostname"] = files["/etc/hostname"].strip()[:255]
    disks = []
    for line in files.get("/proc/diskstats", "").splitlines()[:512]:
        fields = line.split()
        if len(fields) >= 14:
            disks.append(
                {
                    "device": fields[2],
                    "reads_completed": int(fields[3]),
                    "sectors_read": int(fields[5]),
                    "read_milliseconds": int(fields[6]),
                    "writes_completed": int(fields[7]),
                    "sectors_written": int(fields[9]),
                    "write_milliseconds": int(fields[10]),
                    "in_flight": int(fields[11]),
                    "io_milliseconds": int(fields[12]),
                    "weighted_io_milliseconds": int(fields[13]),
                }
            )
    values["disk_counters"] = disks
    return values


def collect_host(ssh, context=None):
    context = context or {}
    deadline = context.get("deadline", context.get("deadline_monotonic"))
    files = {}
    failures = []
    truncated = False
    started_ns = time.perf_counter_ns()
    with ssh.open_sftp() as sftp:
        if hasattr(sftp, "get_channel"):
            sftp.get_channel().settimeout(remaining(deadline, 3))
        for path in FILES:
            try:
                remaining(deadline)
                with sftp.open(path, "rb") as stream:
                    value = stream.read(FILE_LIMIT + 1)
                if len(value) > FILE_LIMIT:
                    truncated = True
                    failures.append({"source": path, "error_code": "byte_limit"})
                files[path] = value[:FILE_LIMIT].decode("utf-8", errors="replace")
            except Exception as exc:
                failures.append({"source": path, "error_code": safe_error_code(exc)})
    values = parse_host_files(files)
    # Fixed mount only. It does not claim all data directory mounts are covered.
    try:
        remaining(deadline)
        stdin, stdout, stderr = ssh.exec_command(DF_COMMAND, timeout=remaining(deadline, 3))
        stdin.close()
        raw = stdout.read(8193)
        if len(raw) > 8192:
            raise ValueError("df_limit")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        status = stdout.channel.recv_exit_status()
        if status != 0 or len(lines) < 2:
            raise ValueError("df_failed")
        row = lines[-1].split()
        values["root_filesystem"] = {
            "size_bytes": int(row[1]) * 1024,
            "used_bytes": int(row[2]) * 1024,
            "available_bytes": int(row[3]) * 1024,
            "mount": "/",
            "coverage": "root_mount_only",
        }
        stdout.close()
        stderr.close()
    except Exception as exc:
        failures.append({"source": "df_root", "error_code": safe_error_code(exc)})
    item = result(
        "linux.host",
        {"values": values, "failures": failures},
        availability="degraded" if failures else "available",
        truncated=truncated,
        coverage={
            "complete": not failures,
            "protocol": "fixed-linux-proc-v1",
            "disk_space_mounts": ["/"],
        },
    )
    item["metric_frame"] = build_host_frame(
        values, context, item["observed_at"], started_ns, item["availability"]
    )
    return item


def build_host_frame(values, context, observed_at, observed_monotonic_ns, availability):
    frame = {
        "instance_id": context.get("instance_id"),
        "server_uuid": context.get("server_uuid"),
        "owner_collector": "host",
        "collector": "host",
        "collector_boot_id": context.get("collector_boot_id", BOOT_ID),
        "counter_epoch": int(context.get("counter_epoch", 0)),
        "source_epoch": values.get("host_boot_id"),
        "observed_monotonic_ns": observed_monotonic_ns,
        "observed_at": observed_at,
        "uptime_seconds": values.get("uptime_seconds"),
        "raw_values": values,
        "availability": availability,
    }
    derived = derive(context.get("previous_frame"), frame, context.get("interval_seconds", 30))
    frame.update(
        derived_values=derived["values"],
        rate_validity=derived["validity"],
        rate_reasons=derived["reasons"],
        window_seconds=derived["window_seconds"],
        window_validity=derived["window_validity"],
        metric_validity=derived["metric_validity"],
    )
    return frame
