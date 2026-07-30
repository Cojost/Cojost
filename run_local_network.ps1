$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$managePyPath = Join-Path $projectRoot 'manage.py'
$venvRoot = Join-Path $projectRoot '.venv'
$activatePath = Join-Path $venvRoot 'Scripts\Activate.ps1'
$venvPythonPath = Join-Path $venvRoot 'Scripts\python.exe'

if (-not (Test-Path -Path $managePyPath -PathType Leaf)) {
    Write-Error "manage.py was not found at: $managePyPath"
    exit 1
}

if (-not (Test-Path -Path $venvRoot -PathType Container)) {
    Write-Error ".venv was not found at: $venvRoot"
    exit 1
}

if (-not (Test-Path -Path $activatePath -PathType Leaf)) {
    Write-Error "Virtual environment activation script was not found: $activatePath"
    exit 1
}

if (-not (Test-Path -Path $venvPythonPath -PathType Leaf)) {
    Write-Error "Python executable was not found in .venv: $venvPythonPath"
    exit 1
}

Set-Location $projectRoot
. $activatePath

$privateAddress = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object {
        $_.IPAddress -notlike '169.254.*' -and
        (
            $_.IPAddress -like '10.*' -or
            $_.IPAddress -like '192.168.*' -or
            $_.IPAddress -like '172.16.*' -or
            $_.IPAddress -like '172.17.*' -or
            $_.IPAddress -like '172.18.*' -or
            $_.IPAddress -like '172.19.*' -or
            $_.IPAddress -like '172.2?.*' -or
            $_.IPAddress -like '172.30.*' -or
            $_.IPAddress -like '172.31.*'
        )
    } |
    Sort-Object -Property InterfaceMetric, SkipAsSource |
    Select-Object -First 1 -ExpandProperty IPAddress

if (-not $privateAddress) {
    Write-Error 'No active private IPv4 address was detected. Ensure Wi-Fi is connected and retry.'
    exit 1
}

Write-Host ''
Write-Host 'Desktop and phone must be on the same Wi-Fi network.' -ForegroundColor Yellow
Write-Host "Open this URL on your phone: http://$privateAddress`:8000/" -ForegroundColor Green
Write-Host ''
Write-Host 'Starting Django development server on 0.0.0.0:8000 ...' -ForegroundColor Cyan

python manage.py runserver 0.0.0.0:8000
