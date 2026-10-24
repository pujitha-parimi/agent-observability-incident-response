param(
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$pythonExe = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$prometheusExe = Join-Path $ProjectRoot "prometheus\prometheus\prometheus.exe"
$prometheusConfig = Join-Path $ProjectRoot "prometheus\prometheus\prometheus.yml"
$prometheusData = Join-Path $ProjectRoot "prometheus\data"
$runtimeDir = Join-Path $ProjectRoot "runtime"
$logDir = Join-Path $ProjectRoot "logs"
$manifestPath = Join-Path $runtimeDir "managed-processes.json"

foreach ($path in @($pythonExe, $prometheusExe, $prometheusConfig)) {
    if (-not (Test-Path $path)) {
        throw "Required project file not found: $path"
    }
}

New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir, $prometheusData | Out-Null

$managedProcesses = @()
if (Test-Path $manifestPath) {
    try {
        $existingManifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
        $managedProcesses = @($existingManifest.processes)
    } catch {
        Write-Warning "Could not read the prior process manifest. No unverified processes will be stopped."
    }
}

function Get-PortListeners([int]$Port) {
    try {
        return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Test-AgentEndpoint {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        return ($health.status -eq "ok" -and $health.service -eq "agent-observability-system")
    } catch {
        return $false
    }
}

function Get-PrometheusTargets {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:9090/api/v1/targets" -TimeoutSec 2
        if ($response.status -eq "success") {
            return @($response.data.activeTargets)
        }
    } catch {}
    return @()
}

function Save-ManagedProcesses {
    $manifest = [pscustomobject]@{ processes = @($managedProcesses) }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding UTF8
}

$existingTargets = Get-PrometheusTargets
$agentTarget = $existingTargets | Where-Object { $_.labels.job -eq "agent-observability" } | Select-Object -First 1
if (-not $agentTarget) {
    $promListeners = Get-PortListeners 9090
    if ($promListeners.Count -gt 0) {
        throw "Port 9090 is already in use by PID(s) $($promListeners -join ', '), but Prometheus does not expose the expected agent target. The process was not changed."
    }
}

$startedAgent = $false
$startedPrometheus = $false

if (Test-AgentEndpoint) {
    Write-Output "Agent already responds at http://localhost:8000."
} else {
    $agentListeners = Get-PortListeners 8000
    if ($agentListeners.Count -gt 0) {
        throw "Port 8000 is already in use by PID(s) $($agentListeners -join ', '), but the agent health check did not match. Stop or inspect that process; it was not changed."
    }

    $agentProcess = Start-Process -FilePath $pythonExe `
        -ArgumentList @("-m", "agent.agent") `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput (Join-Path $logDir "agent.log") `
        -RedirectStandardError (Join-Path $logDir "agent-error.log") `
        -PassThru

    $startedAgent = $true
    $managedProcesses += [pscustomobject]@{
        role = "agent"
        pid = $agentProcess.Id
        executable = $pythonExe
        commandMarker = "-m agent.agent"
    }
    Save-ManagedProcesses

    $agentReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($agentProcess.HasExited) {
            throw "Agent exited during startup. Review logs\agent-error.log and logs\agent.log. Run .\stop_project.ps1 to clean up any project processes that did start."
        }
        if (Test-AgentEndpoint) {
            $agentReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $agentReady) {
        throw "Agent did not become healthy within 30 seconds. Review logs\agent-error.log. Run .\stop_project.ps1 to clean up any project processes that did start."
    }
    Write-Output "Agent is healthy at http://localhost:8000/health."

    $toolMetric = ""
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try {
            $metricResponse = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/metrics" -TimeoutSec 2
            $toolMetric = ($metricResponse.Content -split "`n" |
                Where-Object { $_ -match '^agent_tool_failures_total\{.*\}\s+[1-9]' } |
                Select-Object -First 1)
            if ($toolMetric) { break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if ($toolMetric) {
        Write-Output "Tool-failure metric is populated: $toolMetric"
    } else {
        Write-Warning "Agent is healthy, but no nonzero tool-failure sample is currently present at /metrics."
    }
}

if ($agentTarget) {
    Write-Output "Prometheus already has the agent-observability target configured (health: $($agentTarget.health))."
} else {
    $prometheusArguments = @(
        "--config.file=`"$prometheusConfig`"",
        "--storage.tsdb.path=`"$prometheusData`""
    )
    $prometheusProcess = Start-Process -FilePath $prometheusExe `
        -ArgumentList $prometheusArguments `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput (Join-Path $logDir "prometheus.log") `
        -RedirectStandardError (Join-Path $logDir "prometheus-error.log") `
        -PassThru

    $startedPrometheus = $true
    $managedProcesses += [pscustomobject]@{
        role = "prometheus"
        pid = $prometheusProcess.Id
        executable = $prometheusExe
        commandMarker = "--config.file=`"$prometheusConfig`""
    }
    Save-ManagedProcesses
}

$prometheusReady = $false
$expectedAlerts = @("AgentHighErrorRate", "AgentHighLatency", "AgentToolFailureSpike")
for ($attempt = 0; $attempt -lt 45; $attempt++) {
    $targets = Get-PrometheusTargets
    $agentTarget = $targets | Where-Object { $_.labels.job -eq "agent-observability" } | Select-Object -First 1
    try {
        $rulesResponse = Invoke-RestMethod -Uri "http://127.0.0.1:9090/api/v1/rules" -TimeoutSec 2
        $loadedNames = @($rulesResponse.data.groups | ForEach-Object { $_.rules } | ForEach-Object { $_.name })
        $rulesLoaded = @($expectedAlerts | Where-Object { $loadedNames -contains $_ }).Count -eq $expectedAlerts.Count
    } catch {
        $rulesLoaded = $false
    }

    if ($agentTarget -and $agentTarget.health -eq "up" -and $rulesLoaded) {
        $prometheusReady = $true
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $prometheusReady) {
    throw "Prometheus did not confirm an UP agent target and all three alert rules within 45 seconds. Review logs\prometheus-error.log, then run .\stop_project.ps1 to clean up any project processes that did start."
}

if ($startedAgent -or $startedPrometheus) {
    $manifest = [pscustomobject]@{ processes = @($managedProcesses) }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding UTF8
}

Write-Output "Prometheus is ready at http://localhost:9090."
Write-Output "Agent scrape target is UP; all three alert rules are loaded."
Write-Output "Metrics: http://localhost:8000/metrics | Alerts: http://localhost:9090/alerts"
Write-Output "Logs: $logDir"
Start-Process "http://localhost:9090/targets"
