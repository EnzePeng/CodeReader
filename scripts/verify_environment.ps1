$ErrorActionPreference = "Stop"
$workspace = Split-Path $PSScriptRoot -Parent

Push-Location $workspace
try {
    $pythonVersion = uv run --frozen python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($pythonVersion.Trim() -ne "3.13") { throw "Python 3.13 is required" }

    $nodeVersion = (node --version).TrimStart('v')
    if ([int]$nodeVersion.Split('.')[0] -lt 24) { throw "Node.js 24 LTS is required" }

    uv run --frozen ruff check backend
    uv run --frozen mypy backend/app
    uv run --frozen python -m unittest discover -s backend -p "test_*.py"

    Push-Location frontend
    try {
        npm ci
        npm run typecheck
        npm test
        npm run build
        npm audit --audit-level=high
    } finally {
        Pop-Location
    }
} finally {
    Pop-Location
}
