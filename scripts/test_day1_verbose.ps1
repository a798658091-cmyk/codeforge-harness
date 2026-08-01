param(
    [switch]$LiveProvider
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python virtual environment not found: $python"
}

$day1Tests = @(
    "tests\test_agent_loop.py"
    "tests\test_day1_tools.py"
    "tests\test_permissions.py"
    "tests\test_provider.py"
    "tests\test_state.py"
    "tests\test_tool_validation.py"
    "tests\test_workspace.py"
)

if ($LiveProvider) {
    $day1Tests += "tests\live_provider_check.py"
}

Write-Host "CodeForge Harness - core detailed test run"
Write-Host "Runner workspace: $projectRoot"
if ($LiveProvider) {
    Write-Host "LLM access: ENABLED (one real provider request)"
}
else {
    Write-Host "LLM access: disabled (MockProvider and fake OpenAI client only)"
}

Push-Location $projectRoot
try {
    & $python -m pytest @day1Tests `
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
