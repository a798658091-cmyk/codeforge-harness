# Runs the real-provider demo for writable Subagent, Worktree, and MessageBus.
# This file intentionally stays ASCII-only for Windows PowerShell 5.1.

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$promptPath = Join-Path $PSScriptRoot "demo_subagent_prompt.txt"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Virtual environment Python was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $promptPath)) {
    throw "Demo prompt was not found: $promptPath"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$demoWorkspace = Join-Path $projectRoot ".codeforge\team-demo-$timestamp"
$demoFile = "subagent-demo.md"

New-Item -ItemType Directory -Path $demoWorkspace | Out-Null
Set-Content -LiteralPath (Join-Path $demoWorkspace "README.md") `
    -Value "# CodeForge Team Demo" `
    -Encoding UTF8
& git -C $demoWorkspace init | Out-Null
& git -C $demoWorkspace add README.md
& git -C $demoWorkspace `
    -c user.name="CodeForge Demo" `
    -c user.email="codeforge-demo@local" `
    commit -m "Initialize isolated demo workspace" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not initialize the demo Git repository: $demoWorkspace"
}

$taskTemplate = Get-Content -Raw -Encoding UTF8 -LiteralPath $promptPath
$task = $taskTemplate.Replace("{{DEMO_FILE}}", $demoFile)

Write-Host "Demo workspace: $demoWorkspace"
Push-Location $projectRoot
try {
    & $pythonPath -m harness `
        --workspace $demoWorkspace `
        --yes `
        --max-subagents 1 `
        --max-steps 30 `
        $task
    $harnessExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

$resultPath = Join-Path $demoWorkspace $demoFile
if ($harnessExitCode -eq 0 -and (Test-Path -LiteralPath $resultPath)) {
    Write-Host "`nFinal file: $resultPath"
    Get-Content -Encoding UTF8 -LiteralPath $resultPath
}

exit $harnessExitCode
