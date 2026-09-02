param(
    [switch]$RunBenchmarks
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repositoryRoot

$pairs = @(
    @{ Cameras = 1; Baseline = "shared_inference_perf_1cam.json"; Batch = "shared_batch_perf_1cam.json" },
    @{ Cameras = 2; Baseline = "shared_inference_perf_2cams.json"; Batch = "shared_batch_perf_2cams.json" },
    @{ Cameras = 4; Baseline = "shared_inference_perf_4cams.json"; Batch = "shared_batch_perf_4cams.json" },
    @{ Cameras = 8; Baseline = "shared_inference_perf_8cams.json"; Batch = "shared_batch_perf_8cams.json" }
)

function Invoke-Benchmark([string]$ConfigPath) {
    Write-Host "Running $ConfigPath"
    & python -m fight.pipeline_mp.run_multiprocess --config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "Benchmark failed with exit code ${LASTEXITCODE}: $ConfigPath"
    }
}

function Read-Summary([string]$ConfigPath) {
    $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    $summaryPath = Join-Path $repositoryRoot ([string]$config.output_dir)
    $summaryPath = Join-Path $summaryPath "performance_summary.json"
    if (-not (Test-Path -LiteralPath $summaryPath)) {
        throw "Summary not found: $summaryPath"
    }
    return Get-Content -Raw -LiteralPath $summaryPath | ConvertFrom-Json
}

function Add-ComparisonRow(
    [System.Collections.ArrayList]$Rows,
    [int]$Cameras,
    [string]$Metric,
    [double]$Baseline,
    [double]$Batch
) {
    $deltaPercent = if ($Baseline -ne 0.0) {
        (($Batch - $Baseline) / $Baseline) * 100.0
    } else {
        0.0
    }
    [void]$Rows.Add([pscustomobject]@{
        Cameras = $Cameras
        Metric = $Metric
        Baseline = [math]::Round($Baseline, 3)
        Batch = [math]::Round($Batch, 3)
        DeltaPercent = [math]::Round($deltaPercent, 2)
    })
}

if ($RunBenchmarks) {
    foreach ($pair in $pairs) {
        Invoke-Benchmark $pair.Baseline
        Invoke-Benchmark $pair.Batch
    }
}

$rows = [System.Collections.ArrayList]::new()
foreach ($pair in $pairs) {
    $baseline = Read-Summary $pair.Baseline
    $batch = Read-Summary $pair.Batch
    $cameraCount = [int]$pair.Cameras
    $baselineCameraFps = ($baseline.cameras | Measure-Object -Property camera_processing_fps -Average).Average
    $batchCameraFps = ($batch.cameras | Measure-Object -Property camera_processing_fps -Average).Average

    Add-ComparisonRow $rows $cameraCount "aggregate_processing_fps" $baseline.aggregate_processing_fps $batch.aggregate_processing_fps
    Add-ComparisonRow $rows $cameraCount "mean_camera_processing_fps" $baselineCameraFps $batchCameraFps
    Add-ComparisonRow $rows $cameraCount "per_camera_realtime_factor" $baseline.per_camera_realtime_factor $batch.per_camera_realtime_factor
    Add-ComparisonRow $rows $cameraCount "person.queue_wait_ms.p95" $baseline.person.queue_wait_ms.p95 $batch.person.queue_wait_ms.p95
    Add-ComparisonRow $rows $cameraCount "person.round_trip_ms.p95" $baseline.person.round_trip_ms.p95 $batch.person.round_trip_ms.p95
    Add-ComparisonRow $rows $cameraCount "pose.queue_wait_ms.p95" $baseline.pose.queue_wait_ms.p95 $batch.pose.queue_wait_ms.p95
    Add-ComparisonRow $rows $cameraCount "pose.round_trip_ms.p95" $baseline.pose.round_trip_ms.p95 $batch.pose.round_trip_ms.p95
    Add-ComparisonRow $rows $cameraCount "person.steady_state.queue_wait_ms.p95" $baseline.person.steady_state.queue_wait_ms.p95 $batch.person.steady_state.queue_wait_ms.p95
    Add-ComparisonRow $rows $cameraCount "person.steady_state.round_trip_ms.p95" $baseline.person.steady_state.round_trip_ms.p95 $batch.person.steady_state.round_trip_ms.p95
    Add-ComparisonRow $rows $cameraCount "pose.steady_state.queue_wait_ms.p95" $baseline.pose.steady_state.queue_wait_ms.p95 $batch.pose.steady_state.queue_wait_ms.p95
    Add-ComparisonRow $rows $cameraCount "pose.steady_state.round_trip_ms.p95" $baseline.pose.steady_state.round_trip_ms.p95 $batch.pose.steady_state.round_trip_ms.p95
    Add-ComparisonRow $rows $cameraCount "person.batch.batch_size.mean" 1.0 $batch.person.batch.batch_size.mean
    Add-ComparisonRow $rows $cameraCount "pose.batch.batch_size.mean" 1.0 $batch.pose.batch.batch_size.mean
    Add-ComparisonRow $rows $cameraCount "person.timeouts" $baseline.person.timeouts $batch.person.timeouts
    Add-ComparisonRow $rows $cameraCount "pose.timeouts" $baseline.pose.timeouts $batch.pose.timeouts
    Add-ComparisonRow $rows $cameraCount "person.queue_full" $baseline.person.queue_full $batch.person.queue_full
    Add-ComparisonRow $rows $cameraCount "pose.queue_full" $baseline.pose.queue_full $batch.pose.queue_full
    Add-ComparisonRow $rows $cameraCount "person.result_drops" $baseline.person.result_drops $batch.person.result_drops
    Add-ComparisonRow $rows $cameraCount "pose.result_drops" $baseline.pose.result_drops $batch.pose.result_drops
}

$rows | Format-Table -AutoSize
