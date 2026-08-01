$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python virtual environment not found: $python"
}

Write-Host "CodeForge Harness - Day 1 + Day 2 detailed test run"
Write-Host "Runner workspace: $projectRoot"
Write-Host "LLM access: disabled (MockProvider and fake OpenAI client only)"

Push-Location $projectRoot
try {
    & $python -m pytest tests `
        -vv `
        -s `
        -ra `
        --tb=short `
        --show-test-process `
        -p no:cacheprovider
    $testExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $testExitCode
