param(
    [string]$PackageRoot = "release\CodeReader",
    [int]$Port = 18710
)

$ErrorActionPreference = "Stop"
$workspace = Split-Path $PSScriptRoot -Parent
$target = [IO.Path]::GetFullPath((Join-Path $workspace $PackageRoot))
$expectedRoot = [IO.Path]::GetFullPath((Join-Path $workspace "release"))
if (-not $target.StartsWith($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "PackageRoot must stay inside $expectedRoot"
}
$exe = Join-Path $target "CodeReader.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Missing $exe" }

$process = Start-Process -FilePath $exe -ArgumentList "--port", $Port, "--no-browser" -PassThru -WindowStyle Hidden
try {
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) { throw "CodeReader exited before becoming healthy" }
        try {
            $response = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/" -TimeoutSec 2
            if ($response.StatusCode -eq 200) { break }
        } catch { }
    } while ((Get-Date) -lt $deadline)
    if ((Get-Date) -ge $deadline) { throw "Cold-start smoke test timed out" }
} finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id
        $process.WaitForExit(10000) | Out-Null
    }
}

$orphan = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue |
    Where-Object { $_.StartTime -ge $process.StartTime }
if ($orphan) { throw "Orphan llama-server process detected after shutdown" }
Write-Host "Package cold-start and clean-exit smoke test passed"
