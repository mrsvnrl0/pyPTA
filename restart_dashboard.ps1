<#
.SYNOPSIS
Restart this folder's dashboard without finding its process manually.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\restart_dashboard.ps1
.EXAMPLE
.\restart_dashboard.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateRange(1, 65535)][int]$Port = 5000,
    [ValidatePattern('^[A-Za-z0-9_.:%-]+$')][string]$BindAddress = '0.0.0.0',
    [ValidateSet('auto', 'json', 'sqlite')][string]$StateBackend = 'auto',
    [string]$Python = '',
    [ValidateRange(5, 120)][int]$StartupTimeoutSeconds = 45
)

$ErrorActionPreference = 'Stop'
$projectDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$dashboardScript = Join-Path $projectDirectory 'adaptive_crypto_dashboard.py'
$venvPython = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$probeAddress = switch ($BindAddress) {
    '0.0.0.0' { '127.0.0.1' }
    '::' { '::1' }
    default { $BindAddress }
}
$urlAddress = if ($probeAddress.Contains(':')) { "[$probeAddress]" } else { $probeAddress }
$dashboardUrl = "http://${urlAddress}:$Port"

function Get-DashboardListeners {
    # Query all listeners so a genuine query failure is not mistaken for a free port.
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object LocalPort -eq $Port)
}

function Get-EntrypointArgument($ProcessInfo) {
    if (-not $ProcessInfo -or $ProcessInfo.Name -notmatch '^python(?:w)?\.exe$') { return $null }
    $entrypoint = [regex]::Match($ProcessInfo.CommandLine,
        '(?i)(?:^|\s)(?:"([^"]*adaptive_crypto_dashboard\.py)"|([^\s"]*adaptive_crypto_dashboard\.py))(?=\s|$)')
    if (-not $entrypoint.Success) { return $null }
    $argument = if ($entrypoint.Groups[1].Success) { $entrypoint.Groups[1].Value } else { $entrypoint.Groups[2].Value }
    if ([IO.Path]::GetFileName($argument) -ine 'adaptive_crypto_dashboard.py') { return $null }
    return $argument
}

function Test-ProjectDashboard($ProcessInfo, $ProcessTable) {
    $argument = Get-EntrypointArgument $ProcessInfo
    if (-not $argument) { return $false }
    if ([IO.Path]::IsPathRooted($argument)) {
        return [IO.Path]::GetFullPath($argument) -ieq $dashboardScript
    }
    # Older launchers use a relative script argument. Only accept it when this
    # project's venv executable is the process or a matching Python ancestor.
    if ($argument -notin @('adaptive_crypto_dashboard.py', '.\adaptive_crypto_dashboard.py', './adaptive_crypto_dashboard.py')) {
        return $false
    }
    $candidate = $ProcessInfo
    for ($depth = 0; $candidate -and $depth -lt 8; $depth++) {
        if ((Get-EntrypointArgument $candidate) -ne $argument) { return $false }
        if ($candidate.ExecutablePath -ieq $venvPython) { return $true }
        $candidate = $ProcessTable[[int]$candidate.ParentProcessId]
    }
    return $false
}

function Get-DashboardStatus {
    try {
        Invoke-RestMethod -Uri "$dashboardUrl/api/dashboard/status" -TimeoutSec 2 -ErrorAction Stop
    } catch { return $null }
}

if (-not (Test-Path -LiteralPath $dashboardScript -PathType Leaf)) {
    throw "Dashboard script is missing from $projectDirectory. Keep this restart script in the project folder."
}
if (-not $Python) {
    $Python = if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $venvPython } else { 'python.exe' }
}
$pythonExecutable = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
$processTable = @{}
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    ForEach-Object { $processTable[[int]$_.ProcessId] = $_ }
$listeners = @(Get-DashboardListeners)
$listenerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
$stopProcesses = [Collections.Generic.List[object]]::new()
foreach ($listenerId in $listenerIds) {
    $listenerProcess = $processTable[[int]$listenerId]
    if (-not (Test-ProjectDashboard $listenerProcess $processTable)) {
        throw "Port $Port belongs to process $listenerId, which cannot be verified as this project's dashboard. Nothing was stopped."
    }
    if ($listenerProcess.CommandLine -match '(?i)(?:^|\s)"?--(?:state|settings)(?:=|"|\s|$)') {
        throw 'This dashboard uses custom settings or state paths. Restart it with its original command so those paths are preserved. Nothing was stopped.'
    }
    $stopProcesses.Add($listenerProcess)
    $parent = $processTable[[int]$listenerProcess.ParentProcessId]
    if ($parent -and $parent.ExecutablePath -ieq $venvPython -and (Test-ProjectDashboard $parent $processTable)) {
        if (-not ($stopProcesses | Where-Object ProcessId -eq $parent.ProcessId)) { $stopProcesses.Add($parent) }
    }
}
$previousStatus = if ($listenerIds.Count) { Get-DashboardStatus } else { $null }
$stdout = Join-Path $projectDirectory 'dashboard.log'
$stderr = Join-Path $projectDirectory 'dashboard.error.log'
$processDescription = if ($stopProcesses.Count) { ($stopProcesses.ProcessId -join ', ') } else { 'none; dashboard is stopped' }
Write-Host "Project: $projectDirectory"
Write-Host "Dashboard processes: $processDescription"

if (-not $PSCmdlet.ShouldProcess("$projectDirectory on port $Port", 'Stop verified dashboard processes and start a fresh dashboard')) {
    return
}

foreach ($oldProcess in $stopProcesses) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($oldProcess.ProcessId)"
    # The venv launcher can exit after its child stops, leaving a transient CIM
    # record without readable identity fields. Allow it to disappear, but never
    # stop a process whose creation time and command line cannot be verified.
    for ($attempt = 0; $current -and
        ($null -eq $current.CreationDate -or [string]::IsNullOrWhiteSpace($current.CommandLine)) -and
        $attempt -lt 4; $attempt++) {
        Start-Sleep -Milliseconds 250
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($oldProcess.ProcessId)"
    }
    if ($current) {
        if ($current.CreationDate -ne $oldProcess.CreationDate -or $current.CommandLine -ne $oldProcess.CommandLine) {
            throw "Process $($oldProcess.ProcessId) changed during restart. Stopping here to avoid terminating another program."
        }
        Write-Host "Stopping dashboard process $($oldProcess.ProcessId)..."
        try {
            Stop-Process -Id $oldProcess.ProcessId -Force -ErrorAction Stop
        } catch {
            # A verified venv parent may disappear between the CIM identity
            # check and Stop-Process. Continue only when it is already gone;
            # a surviving process or an access failure still aborts restart.
            $stopFailure = $_
            $stillRunning = Get-Process -Id $oldProcess.ProcessId -ErrorAction SilentlyContinue
            if ($stillRunning) { throw $stopFailure }
        }
        Wait-Process -Id $oldProcess.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
}
$releaseDeadline = [DateTime]::UtcNow.AddSeconds(10)
while (@(Get-DashboardListeners).Count) {
    if ([DateTime]::UtcNow -ge $releaseDeadline) { throw "Port $Port did not become available. No new dashboard was started." }
    Start-Sleep -Milliseconds 250
}

# Use the same startup options as start_dashboard.ps1, with an absolute script
# path so subsequent restarts can verify ownership even outside a virtualenv.
$arguments = '-u "{0}" --host "{1}" --port {2} --state-backend {3}' -f $dashboardScript, $BindAddress, $Port, $StateBackend
$newProcess = Start-Process -FilePath $pythonExecutable -ArgumentList $arguments `
    -WorkingDirectory $projectDirectory -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Write-Host "Starting dashboard (launcher PID $($newProcess.Id))..."
$startupDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
do {
    $newProcess.Refresh()
    if ($newProcess.HasExited) { throw "Dashboard exited with code $($newProcess.ExitCode). Check $stderr." }
    $status = Get-DashboardStatus
    if ($status -and $status.generation -and -not $status.restarting -and
        (-not $previousStatus -or $status.generation -ne $previousStatus.generation)) {
        $newOwners = @(Get-DashboardListeners | Select-Object -ExpandProperty OwningProcess -Unique)
        $childIds = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($newProcess.Id)" |
            Select-Object -ExpandProperty ProcessId)
        if ($newOwners.Count -and @($newOwners | Where-Object { $_ -notin (@($newProcess.Id) + $childIds) }).Count -eq 0) {
            Write-Host "Dashboard restarted: $dashboardUrl/" -ForegroundColor Green
            Write-Host "Logs: $stdout and $stderr"
            Write-Host 'You can close this terminal; the dashboard runs in the background.'
            return
        }
    }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $startupDeadline)
throw "Dashboard did not become ready within $StartupTimeoutSeconds seconds. Check $stderr. Launcher PID: $($newProcess.Id)."
