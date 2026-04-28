# ops-plugin 자동 실행 스크립트 — PowerShell 버전 (v1.0, 260424)
#
# 사용법:
#   .\run_pipeline.ps1 -Stage pre  [-Today "2026-04-27"]
#   .\run_pipeline.ps1 -Stage post [-Today "2026-04-27"]
#
# bash 버전은 run_pipeline.sh 참조.

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("pre", "post")]
    [string]$Stage,
    [string]$Today
)

$ErrorActionPreference = "Stop"

$Base = Split-Path -Parent $PSCommandPath
$Core = $Base
$TodayFmt = Get-Date -Format "yyyyMMdd"
$TodayArgs = @()
if ($Today) { $TodayArgs = @("--today", $Today) }

function Test-RequiredFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Host "[ERROR] 파일 없음: $Path" -ForegroundColor Red
        exit 1
    }
}

function Get-PendingCount {
    param([string]$JsonPath, [string]$MetaKey)
    if (-not (Test-Path $JsonPath)) { return 0 }
    $raw = Get-Content $JsonPath -Raw -Encoding UTF8
    $obj = $raw | ConvertFrom-Json
    if ($obj.meta -and $obj.meta.$MetaKey) { return $obj.meta.$MetaKey }
    return 0
}

function Invoke-Pre {
    Write-Host "════════════════════════════════════════════"
    Write-Host " PRE 단계 시작 (Step 0.5 -> 3)"
    Write-Host "════════════════════════════════════════════"

    foreach ($f in @("s1_deals.json", "s2_calendar.json", "s3_slack.json", "s4_gmail.json", "s5_drive.json")) {
        Test-RequiredFile (Join-Path $Base "runtime\$f")
    }
    Test-RequiredFile (Join-Path $Core "config\checkpoints.json")

    Write-Host ""
    Write-Host "[1/4] Step 0.5 compose_schedule..."
    $opsReqArgs = @()
    $opsReqPath = Join-Path $Base "runtime\s3_slack_ops_requests.json"
    if (Test-Path $opsReqPath) { $opsReqArgs = @("--slack-ops-requests", $opsReqPath) }
    python (Join-Path $Base "scripts\compose_schedule.py") `
        --salesmap (Join-Path $Base "runtime\s1_deals.json") `
        --calendar (Join-Path $Base "runtime\s2_calendar.json") `
        --drive    (Join-Path $Base "runtime\s5_drive.json") `
        --slack    (Join-Path $Base "runtime\s3_slack.json") `
        --out      (Join-Path $Base "runtime\sessions.json") `
        @opsReqArgs
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "[2/4] Step 1 build_matrix..."
    $stateArgs = @()
    $statePath = Join-Path $Base "state\ops_state.json"
    if (Test-Path $statePath) { $stateArgs = @("--state", $statePath) }
    python (Join-Path $Base "scripts\build_matrix.py") `
        --sessions    (Join-Path $Base "runtime\sessions.json") `
        --checkpoints (Join-Path $Core "config\checkpoints.json") `
        --out         (Join-Path $Base "runtime\arriving_cells.json") `
        @stateArgs @TodayArgs
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "[3/4] Step 2 collect_evidence..."
    python (Join-Path $Base "scripts\collect_evidence.py") `
        --cells       (Join-Path $Base "runtime\arriving_cells.json") `
        --checkpoints (Join-Path $Core "config\checkpoints.json") `
        --slack       (Join-Path $Base "runtime\s3_slack.json") `
        --gmail       (Join-Path $Base "runtime\s4_gmail.json") `
        --drive       (Join-Path $Base "runtime\s5_drive.json") `
        --settings    (Join-Path $Core "config\settings.json") `
        --out         (Join-Path $Base "runtime\evidence.json")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "[4/4] Step 3 classify_evidence..."
    python (Join-Path $Base "scripts\classify_evidence.py") `
        --evidence    (Join-Path $Base "runtime\evidence.json") `
        --checkpoints (Join-Path $Core "config\checkpoints.json") `
        --out         (Join-Path $Base "runtime\classified_cells.json") `
        @stateArgs
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "════════════════════════════════════════════"
    Write-Host " PRE 단계 완료"
    Write-Host "════════════════════════════════════════════"

    $slackPend = Get-PendingCount (Join-Path $Base "runtime\sessions.json") "pending_slack_count"
    $labelPend = Get-PendingCount (Join-Path $Base "runtime\classified_cells.json") "pending_llm"

    if ($slackPend -ne 0 -or $labelPend -ne 0) {
        Write-Host ""
        Write-Host "⚠️  PENDING 처리 필요:" -ForegroundColor Yellow
        if ($slackPend -ne 0) { Write-Host "   - 슬랙 일정 변경 후보: $slackPend 딜" }
        if ($labelPend -ne 0) { Write-Host "   - LLM 분류 대기 셀: $labelPend 건" }
        Write-Host ""
        Write-Host "Claude가 각 PENDING에 대해 LLM 호출 후"
        Write-Host "scripts/apply_llm_responses.py --mode {slack|labels} 로 병합 -> post 실행"
    } else {
        Write-Host ""
        Write-Host "✅ PENDING 없음 — 바로 post 실행 가능" -ForegroundColor Green
    }
}

function Invoke-Post {
    Write-Host "════════════════════════════════════════════"
    Write-Host " POST 단계 시작 (Step 4 -> 4.6 -> 슬랙 변환)"
    Write-Host "════════════════════════════════════════════"

    Test-RequiredFile (Join-Path $Base "runtime\classified_cells.json")
    Test-RequiredFile (Join-Path $Base "runtime\sessions.json")

    $labelPend = Get-PendingCount (Join-Path $Base "runtime\classified_cells.json") "pending_llm"
    if ($labelPend -ne 0) {
        Write-Host "⚠️  PENDING 셀 $labelPend 건 잔존 — 🔴 fallback 처리됩니다" -ForegroundColor Yellow
    }

    $outMd = Join-Path $Base "outputs\ops_report_$TodayFmt.md"
    $outSlack = Join-Path $Base "outputs\ops_report_$TodayFmt.slack.txt"

    Write-Host ""
    Write-Host "[1/3] Step 4 generate_ops_md..."
    python (Join-Path $Base "scripts\generate_ops_md.py") `
        --classified  (Join-Path $Base "runtime\classified_cells.json") `
        --checkpoints (Join-Path $Core "config\checkpoints.json") `
        --sessions    (Join-Path $Base "runtime\sessions.json") `
        --evidence    (Join-Path $Base "runtime\evidence.json") `
        --out         $outMd `
        @TodayArgs
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "[2/3] Step 4.6 verify_output_format..."
    python (Join-Path $Base "scripts\verify_output_format.py") --md $outMd --strict
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "[3/3] md_to_slack 변환..."
    python (Join-Path $Base "scripts\md_to_slack.py") $outMd -o $outSlack
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host ""
    Write-Host "════════════════════════════════════════════"
    Write-Host " POST 단계 완료" -ForegroundColor Green
    Write-Host "════════════════════════════════════════════"
    Write-Host "📄 MD:    $outMd"
    Write-Host "💬 Slack: $outSlack"
}

switch ($Stage) {
    "pre"  { Invoke-Pre }
    "post" { Invoke-Post }
}
