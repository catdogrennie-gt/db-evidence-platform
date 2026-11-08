$ErrorActionPreference = 'Stop'
# Stop only this project's loopback Uvicorn listener; leave VM and databases running.
$project = $PSScriptRoot
$listeners = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)"
    if ($process.CommandLine -like '*uvicorn app:app*' -and ($process.CommandLine -like "*$project*" -or $parent.CommandLine -like "*$project*uvicorn app:app*")) {
        Stop-Process -Id $process.ProcessId
        if ($parent.CommandLine -like "*$project*uvicorn app:app*") { Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue }
    } else { throw 'Port 8765 belongs to an unrecognized process; no process was stopped.' }
}
