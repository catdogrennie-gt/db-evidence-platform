param(
    [ValidateSet('Start','Stop','Restart','Status')][string]$Action='Status',
    [ValidateSet('mysql','prometheus','api','worker','ai')][string[]]$Services=@('api','worker','ai'),
    [string]$ConfigPath=''
)
$ErrorActionPreference='Stop'
$platformRepo=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if(-not $ConfigPath){$ConfigPath=Join-Path (Split-Path $platformRepo -Parent) 'platform-runtime\config.json'}
$ConfigPath=(Resolve-Path -LiteralPath $ConfigPath).Path
$platformConfig=Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$platformPrivate=[IO.Path]::GetFullPath($platformConfig.private_dir)
if($platformPrivate.StartsWith($platformRepo+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
    throw 'Private runtime must be outside the source repository.'
}
$platformComponents=Join-Path (Split-Path $platformRepo -Parent) 'components'
$platformMysqlDir=Join-Path $platformComponents 'mysql-8.4.13-winx64'
$platformPrometheusDir=Join-Path $platformComponents 'prometheus-3.13.5.windows-amd64'
if($platformConfig.components.mysql_dir){$platformMysqlDir=[IO.Path]::GetFullPath($platformConfig.components.mysql_dir)}
if($platformConfig.components.prometheus_dir){$platformPrometheusDir=[IO.Path]::GetFullPath($platformConfig.components.prometheus_dir)}
$platformPrometheusConfig=Join-Path $platformRepo 'infra\prometheus\prometheus.yml'
if($platformConfig.prometheus_config_path){$platformPrometheusConfig=[IO.Path]::GetFullPath($platformConfig.prometheus_config_path)}
$platformPrometheusUri=[Uri]$platformConfig.prometheus_url
if($platformPrometheusUri.Host -notin @('localhost','127.0.0.1','::1') -or $platformPrometheusUri.Scheme -ne 'http'){
    throw 'Managed Prometheus requires a loopback HTTP endpoint.'
}
$platformPrometheusListen='127.0.0.1:'+$platformPrometheusUri.Port
if($platformConfig.prometheus_listen_address){
    if($platformConfig.prometheus_listen_address -ne $platformPrometheusListen){throw 'Managed Prometheus listen address must match the configured loopback port.'}
}
$platformPython=Join-Path $platformRepo '.venv\Scripts\python.exe'
$platformPort=([Uri]$platformConfig.public_origin).Port
$env:DBA_PLATFORM_CONFIG=$ConfigPath
foreach($platformDirectory in @('logs','pids')){New-Item -ItemType Directory -Path (Join-Path $platformPrivate $platformDirectory) -Force | Out-Null}

function Get-PlatformSpec([string]$Name){
    switch($Name){
        'mysql' {return @{exe=(Join-Path $platformMysqlDir 'bin\mysqld.exe');module='mysqld';args=@('--defaults-file='+((Join-Path $platformPrivate 'my.ini') -replace '\\','/'))}}
        'prometheus' {return @{exe=(Join-Path $platformPrometheusDir 'prometheus.exe');module='prometheus';args=@(('--config.file='+($platformPrometheusConfig -replace '\\','/')),('--storage.tsdb.path='+((Join-Path $platformPrivate 'prometheus-data') -replace '\\','/')),'--storage.tsdb.retention.time=14d',('--web.listen-address='+$platformPrometheusListen))}}
        'api' {return @{exe=$platformPython;module='uvicorn';args=@('-m','uvicorn','platform_app.api:create_app','--factory','--host','127.0.0.1','--port',[string]$platformPort,'--no-access-log')}}
        'worker' {return @{exe=$platformPython;module='platform_app.worker';args=@('-m','platform_app.worker','--config',$ConfigPath)}}
        'ai' {return @{exe=$platformPython;module='platform_app.ai_worker';args=@('-m','platform_app.ai_worker','--config',$ConfigPath)}}
    }
}

function Get-VerifiedPlatformProcess([string]$Name,$Spec){
    $platformRecordPath=Join-Path $platformPrivate ('pids\'+$Name+'.json')
    if(-not (Test-Path -LiteralPath $platformRecordPath)){return $null}
    $platformRecord=Get-Content -LiteralPath $platformRecordPath -Raw | ConvertFrom-Json
    $platformProcess=Get-CimInstance Win32_Process -Filter ('ProcessId = '+[int]$platformRecord.pid)
    if(-not $platformProcess){return $null}
    if($platformProcess.ExecutablePath -ne $Spec.exe -or $platformRecord.exe -ne $Spec.exe -or
        $platformRecord.module -ne $Spec.module){
        throw ('Process identity mismatch for '+$Name+'; refused action.')
    }
    $platformExpectedArgs=($Spec.args | ForEach-Object {
        $platformLiteral=[Regex]::Escape([string]$_)
        '(?:"'+$platformLiteral+'"|'+$platformLiteral+')'
    }) -join '\s+'
    if($platformProcess.CommandLine -notmatch ('^\s*(?:"[^"]+"|\S+)\s+'+$platformExpectedArgs+'\s*$')){
        throw ('Exact process arguments differ for '+$Name+'; refused action.')
    }
    $platformCreated=([DateTime]$platformProcess.CreationDate).ToUniversalTime()
    if($platformRecord.creation_utc){
        $platformExpectedCreated=([DateTimeOffset]$platformRecord.creation_utc).UtcDateTime
        $platformTolerance=0.001
    }else{
        # Legacy local records predate the exact creation-time field. The
        # launch timestamp still rejects a recycled PID, within bounded startup.
        $platformExpectedCreated=([DateTimeOffset]$platformRecord.started_at).UtcDateTime
        $platformTolerance=5
    }
    if([Math]::Abs(($platformCreated-$platformExpectedCreated).TotalSeconds) -gt $platformTolerance){
        throw ('Process creation time differs for '+$Name+'; refused recycled PID.')
    }
    if($platformRecord.config_path -and $platformRecord.config_path -ne $ConfigPath){
        throw ('Private configuration differs for '+$Name+'; refused action.')
    }
    return $platformProcess
}

function Stop-PlatformService([string]$Name,$Spec){
    $platformProcess=Get-VerifiedPlatformProcess $Name $Spec
    if(-not $platformProcess){return}
    if($Name -eq 'mysql'){
        # Graceful shutdown uses a short-lived private option file, never a
        # password on the command line or a hard kill of a live datadir.
        $platformBootstrap=Get-Content -LiteralPath (Join-Path $platformPrivate 'bootstrap-private.json') -Raw | ConvertFrom-Json
        $platformOption=Join-Path $platformPrivate ('mysql-stop-'+[Guid]::NewGuid().ToString('N')+'.cnf')
        try{
            @('[client]','user=root',('password='+$platformBootstrap.mysql_root.password),'host=127.0.0.1',('port='+$platformConfig.control_db.port),'connect-timeout=5') | Set-Content -LiteralPath $platformOption -Encoding ascii
            & (Join-Path (Split-Path $Spec.exe -Parent) 'mysqladmin.exe') ('--defaults-extra-file='+$platformOption) shutdown
            if($LASTEXITCODE -ne 0){throw 'Control MySQL graceful shutdown failed; not force-stopping.'}
        }finally{Remove-Item -LiteralPath $platformOption -ErrorAction SilentlyContinue}
    }else{
        # A Windows venv launcher owns a separate Python child. Verify its
        # parent and exact module before stopping both, leaving unrelated apps.
        $platformChildren=Get-CimInstance Win32_Process | Where-Object {
            $_.ParentProcessId -eq $platformProcess.ProcessId -and $_.Name -eq 'python.exe' -and
            $_.CommandLine -match ('-m\s+'+[Regex]::Escape($Spec.module)+'(?:\s|$)')
        }
        foreach($platformChild in $platformChildren){Stop-Process -Id $platformChild.ProcessId -ErrorAction SilentlyContinue}
        Stop-Process -Id $platformProcess.ProcessId -ErrorAction SilentlyContinue
    }
    Wait-Process -Id $platformProcess.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
}

function Start-PlatformService([string]$Name,$Spec){
    if(Get-VerifiedPlatformProcess $Name $Spec){return}
    if(-not (Test-Path -LiteralPath $Spec.exe)){throw ('Required official component missing: '+$Spec.exe)}
    $platformStamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
    $platformStdout=Join-Path $platformPrivate ('logs\'+$Name+'-'+$platformStamp+'-stdout.log')
    $platformStderr=Join-Path $platformPrivate ('logs\'+$Name+'-'+$platformStamp+'-stderr.log')
    $platformArguments=($Spec.args | ForEach-Object {'"'+([string]$_).Replace('"','\"')+'"'}) -join ' '
    $platformNew=Start-Process -FilePath $Spec.exe -ArgumentList $platformArguments -WorkingDirectory $platformRepo -WindowStyle Hidden -RedirectStandardOutput $platformStdout -RedirectStandardError $platformStderr -PassThru
    $platformCreated=(Get-CimInstance Win32_Process -Filter ('ProcessId = '+$platformNew.Id)).CreationDate
    @{pid=$platformNew.Id;exe=$Spec.exe;module=$Spec.module;config_path=$ConfigPath;creation_utc=$platformCreated.ToUniversalTime().ToString('o');started_at=[DateTime]::UtcNow.ToString('o');stdout=$platformStdout;stderr=$platformStderr} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $platformPrivate ('pids\'+$Name+'.json'))
}

$platformOrder=@('mysql','prometheus','api','worker','ai') | Where-Object {$_ -in $Services}
if($Action -in @('Stop','Restart')){
    $platformReverse=@($platformOrder);[Array]::Reverse($platformReverse)
    foreach($platformName in $platformReverse){Stop-PlatformService $platformName (Get-PlatformSpec $platformName)}
}
if($Action -in @('Start','Restart')){
    foreach($platformName in $platformOrder){Start-PlatformService $platformName (Get-PlatformSpec $platformName)}
}
foreach($platformName in $platformOrder){
    $platformRunning=Get-VerifiedPlatformProcess $platformName (Get-PlatformSpec $platformName)
    [PSCustomObject]@{service=$platformName;running=[bool]$platformRunning;pid=if($platformRunning){$platformRunning.ProcessId}else{$null}}
}
