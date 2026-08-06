param(
    [ValidateSet("smoke", "collect", "full", "upstream-collect")]
    [string]$Mode = "smoke",
    [string]$ProjectRoot = "",
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectRoot = (Resolve-Path $ProjectRoot).Path
}

Set-Location $ProjectRoot

$tmpRoot = Join-Path $ProjectRoot ".tmp\pytest"
$runId = $PID.ToString() + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$sessionRoot = Join-Path $tmpRoot ("session-" + $runId)
$sessionTmp = Join-Path $sessionRoot "tmp"
$baseTemp = Join-Path $sessionTmp "basetemp"
$outerSnapshot = Join-Path $sessionRoot "outer-real-home-before.json"
$stateGuard = Join-Path $PSScriptRoot "test_state_guard.py"

New-Item -ItemType Directory -Path $sessionTmp -Force | Out-Null

Write-Host "ProjectRoot: $ProjectRoot"
Write-Host "SessionRoot: $sessionRoot"
Write-Host "Mode: $Mode"

# The outer snapshot is intentionally made with -S so sitecustomize cannot
# redirect HOME before the real host paths have been recorded.
& $PythonExecutable -S -X utf8 $stateGuard snapshot --output $outerSnapshot
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to snapshot real host state."
    exit 2
}

$testExit = 2
$guardExit = 2
try {
    $separator = [IO.Path]::PathSeparator
    $pythonEntries = @($ProjectRoot, (Join-Path $ProjectRoot "scripts"))
    if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
        $pythonEntries += $env:PYTHONPATH
    }
    $env:PYTHONPATH = $pythonEntries -join $separator
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:WEBNOVEL_TEST_SESSION_ROOT = $sessionRoot
    $env:TMP = $sessionTmp
    $env:TEMP = $sessionTmp
    $env:TMPDIR = $sessionTmp

    # Ensure a stale isolation marker inherited from a parent test cannot make
    # this new pytest process skip its own early real-home snapshot.
    Remove-Item Env:WEBNOVEL_TEST_ISOLATION -ErrorAction SilentlyContinue
    Remove-Item Env:WEBNOVEL_TEST_REAL_HOME_SNAPSHOT -ErrorAction SilentlyContinue
    Remove-Item Env:WEBNOVEL_TEST_REAL_PATH_CONTEXT -ErrorAction SilentlyContinue

    $tempProbe = @'
import tempfile
from pathlib import Path
d = Path(tempfile.mkdtemp(prefix="webnovel_writer_pytest_"))
list(d.iterdir())
(d / "probe.txt").write_text("ok", encoding="utf-8")
'@
    $tempProbe | & $PythonExecutable -X utf8 - 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Python temporary-directory probe failed."
        $testExit = 1
    } else {
        $commonIsolatedArgs = @(
            "-X", "utf8", "-m", "pytest",
            "-o", "addopts=",
            "-p", "scripts.pytest_bootstrap",
            "-p", "pytest_asyncio.plugin",
            "-p", "pytest_timeout",
            "-p", "no:cacheprovider",
            "--strict-markers",
            "--timeout=30",
            "--timeout-method=thread",
            "--basetemp", $baseTemp
        )

        switch ($Mode) {
            "smoke" {
                $pytestArgs = $commonIsolatedArgs + @(
                    "-q",
                    "scripts/data_modules/tests/test_extract_chapter_context.py",
                    "scripts/data_modules/tests/test_rag_adapter.py"
                )
            }
            "collect" {
                $pytestArgs = $commonIsolatedArgs + @(
                    "--collect-only", "-q",
                    "scripts/data_modules/tests", "scripts/tests"
                )
            }
            "upstream-collect" {
                $pytestArgs = $commonIsolatedArgs + @(
                    "--collect-only", "-q", "-m", "upstream_contract",
                    "scripts/data_modules/tests", "scripts/tests"
                )
            }
            "full" {
                # pytest.ini selects the current Codex-valid contracts and
                # retains the 90% runtime coverage gate.
                $pytestArgs = @(
                    "-X", "utf8", "-m", "pytest",
                    "--basetemp", $baseTemp
                )
            }
        }

        & $PythonExecutable @pytestArgs
        $testExit = $LASTEXITCODE
    }
} catch {
    Write-Host ("Test runner failed: " + $_.Exception.Message)
    $testExit = 2
} finally {
    # This runs even if pytest-timeout terminates the Python process directly.
    & $PythonExecutable -S -X utf8 $stateGuard verify --input $outerSnapshot
    $guardExit = $LASTEXITCODE
}

if ($guardExit -ne 0) {
    Write-Host "Real host state guard failed."
    exit $guardExit
}

exit $testExit
