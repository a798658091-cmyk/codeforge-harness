$projectRoot = Split-Path -Parent $PSScriptRoot
$taskWorkspace = Join-Path $projectRoot ".codeforge\web-demo-workspace"
$taskPort = 8766
$taskMaxSteps = 8
New-Item -ItemType Directory -Force -Path $taskWorkspace | Out-Null

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python virtual environment not found: $python"
}

Write-Host "CodeForge multi-turn UI: http://127.0.0.1:$taskPort"
Write-Host "Workspace: $taskWorkspace"
& $python -m harness.web --workspace $taskWorkspace --port $taskPort --max-steps $taskMaxSteps
exit $LASTEXITCODE
