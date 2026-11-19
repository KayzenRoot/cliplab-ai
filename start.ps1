$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    $python = $launcher.Source
    $pythonArgs = @('-3.12')
} else {
    $python = (Get-Command python -ErrorAction Stop).Source
    $pythonArgs = @()
}
& $python @pythonArgs -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 'Instale Python 3.12.')"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
foreach ($tool in 'ffmpeg', 'ffprobe') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool ausente. Execute: winget install --id Gyan.FFmpeg --exact"
    }
}
$filters = ffmpeg -hide_banner -filters
$encoders = ffmpeg -hide_banner -encoders
if (-not ($filters -match '\bass\b') -or -not ($encoders -match '\blibx264\b')) {
    throw 'Use uma distribuição do FFmpeg com libass e libx264.'
}

$venv = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venv)) {
    & $python @pythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $venv -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venv -m uvicorn cliplab.app:create_app --factory --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
