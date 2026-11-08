$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" resume_environment.py
if ($LASTEXITCODE -ne 0) { throw '实验环境未就绪，检查本地 runtime 配置与虚拟机。' }
$listeners = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    foreach ($listener in $listeners) {
        $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $serviceParent = Get-CimInstance Win32_Process -Filter "ProcessId=$($serviceProcess.ParentProcessId)"
        if ($serviceProcess.CommandLine -notlike '*uvicorn app:app*' -or ($serviceProcess.CommandLine -notlike "*$PSScriptRoot*" -and $serviceParent.CommandLine -notlike "*$PSScriptRoot*uvicorn app:app*")) {
            throw '端口8765被其他程序占用；没有终止任何程序。'
        }
    }
    Write-Host '工作台已在运行：http://127.0.0.1:8765'
    return
}
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8765
