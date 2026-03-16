# Run all tests for the A2A Octane Wrapper.
#
# Usage:
#   .\run_tests.ps1              # all tests (unit + E2E)
#   .\run_tests.ps1 unit         # unit tests only (free, no API key)
#   .\run_tests.ps1 e2e          # E2E tests only (needs GEMINI_API_KEY in .env)
#   .\run_tests.ps1 e2e-free     # E2E tool-routing tests only (free)

param(
    [ValidateSet("all", "unit", "e2e", "e2e-free")]
    [string]$Suite = "all"
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot

# Prefer the workspace virtualenv if present to ensure consistent Python/runtime
$venvPython = Join-Path $PSScriptRoot ".venv-1\Scripts\python.exe"
if (Test-Path $venvPython) {
    # Use the raw path (no extra quotes) so the call operator & can execute it
    $PythonCmd = $venvPython
} else {
    $PythonCmd = "python"
}

try {
    switch ($Suite) {
        "unit" {
            Write-Host "=== Running unit tests ===" -ForegroundColor Cyan
            # Use the tests directory rather than a shell glob so PowerShell
            # doesn't pass a literal 'tests/test_*.py' string to pytest.
            & $PythonCmd -m pytest tests -v
        }
        "e2e" {
            Write-Host "=== Running E2E tests (requires GEMINI_API_KEY) ===" -ForegroundColor Cyan
            & $PythonCmd -m pytest tests/e2e/ -v
        }
        "e2e-free" {
            Write-Host "=== Running E2E tool-routing tests (free, no API key) ===" -ForegroundColor Cyan
            & $PythonCmd -m pytest tests/e2e/ -k "TestToolCorrectness and not DeepEval" -v
        }
        "all" {
            Write-Host "=== Running unit tests ===" -ForegroundColor Cyan
            & $PythonCmd -m pytest tests -v
            Write-Host ""
            Write-Host "=== Running E2E tests ===" -ForegroundColor Cyan
            & $PythonCmd -m pytest tests/e2e/ -v
        }
    }
}
finally {
    Pop-Location
}
