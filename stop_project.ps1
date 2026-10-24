param(
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$manifestPath = Join-Path $ProjectRoot "runtime\managed-processes.json"

if (-not (Test-Path $manifestPath)) {
    Write-Output "No processes launched by start_project.ps1 are recorded. No processes were stopped."
    return
}

$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$remainingProcesses = @()
foreach ($entry in @($manifest.processes)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)" -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Output "$($entry.role): process $($entry.pid) is already stopped."
        continue
    }

    $sameExecutable = [string]::Equals(
        $process.ExecutablePath,
        $entry.executable,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    $sameCommand = $process.CommandLine -and $process.CommandLine.Contains($entry.commandMarker)
    if ($sameExecutable -and $sameCommand) {
        Stop-Process -Id ([int]$entry.pid) -ErrorAction Stop
        Write-Output "Stopped $($entry.role) process $($entry.pid)."
    } else {
        Write-Warning "Did not stop PID $($entry.pid): its executable or command line no longer matches the recorded project process."
        $remainingProcesses += $entry
    }
}

if ($remainingProcesses.Count -gt 0) {
    [pscustomobject]@{ processes = @($remainingProcesses) } |
        ConvertTo-Json -Depth 5 |
        Set-Content -Path $manifestPath -Encoding UTF8
    Write-Warning "Unverified process records were retained for safety. No further processes were stopped."
} else {
    Remove-Item $manifestPath -ErrorAction SilentlyContinue
    Write-Output "Project process cleanup complete. Log files are retained in the logs folder."
}
