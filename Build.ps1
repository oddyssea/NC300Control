$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) { throw 'Create the development environment before building. See README.md.' }
& $buildPython 'tools/build.py'
if ($LASTEXITCODE -ne 0) { throw 'The executable build failed.' }
