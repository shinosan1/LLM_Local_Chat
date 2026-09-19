param(
    [ValidateSet('auto', 'cu124', 'cpu')][string]$Backend = 'auto'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = $PSScriptRoot
$venvDir = Join-Path $root '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$stateFile = Join-Path $venvDir 'llm-local-chat-setup.json'
$appFile = Join-Path $root 'LLM_Local_Chat.py'

function Run-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $File"
    }
}

function Test-Python312X64([string]$File, [string[]]$Prefix) {
    try {
        $args = @()
        $args += $Prefix
        $args += @(
            '-c',
            "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') * 8 == 64 else 1)"
        )
        & $File @args *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python312X64 {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py -and (Test-Python312X64 $py.Source @('-3.12'))) {
        return @{ File = $py.Source; Prefix = @('-3.12') }
    }

    foreach ($name in @('python.exe', 'python3.12.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Python312X64 $cmd.Source @())) {
            return @{ File = $cmd.Source; Prefix = @() }
        }
    }

    return $null
}

try {
    Set-Location -LiteralPath $root

    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'This launcher requires x64 Windows.'
    }
    if (-not (Test-Path -LiteralPath $appFile)) {
        throw 'LLM_Local_Chat.py was not found. Keep the launcher in the application root folder.'
    }

    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null

    Write-Host 'Easy LLM Local Chat - first setup can take a while.'
    Write-Host "Application folder: $root"

    if ($Backend -eq 'auto') {
        $Backend = 'cpu'
        $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if ($smi) {
            $gpuInfo = & $smi.Source --query-gpu=name --format=csv,noheader 2>$null
            if ($LASTEXITCODE -eq 0 -and $gpuInfo) {
                $Backend = 'cu124'
            }
        }
    }

    $requirementsName = if ($Backend -eq 'cu124') { 'requirements.txt' } else { 'requirements-cpu.txt' }
    $requirementsPath = Join-Path $root $requirementsName
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        throw "$requirementsName was not found."
    }

    Write-Host "[1/4] Backend: $Backend"

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host '[2/4] Creating isolated Python 3.12 environment...'
        $python = Find-Python312X64
        if (-not $python) {
            throw 'Python 3.12 x64 was not found. Install Python 3.12.10 x64, then run this batch again.'
        }
        $createArgs = @()
        $createArgs += $python.Prefix
        $createArgs += @('-m', 'venv', $venvDir)
        Run-Checked $python.File $createArgs
    } else {
        Write-Host '[2/4] Existing .venv found.'
    }

    if (-not (Test-Python312X64 $venvPython @())) {
        throw 'The existing .venv is not Python 3.12 x64. Rename or remove .venv, then run this batch again.'
    }

    $requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $needsInstall = $true
    if (Test-Path -LiteralPath $stateFile) {
        try {
            $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
            if ($state.backend -eq $Backend -and
                $state.requirements -eq $requirementsName -and
                $state.sha256 -eq $requirementsHash) {
                $needsInstall = $false
            }
        } catch {
            $needsInstall = $true
        }
    }

    if ($needsInstall) {
        Write-Host "[3/4] Installing dependencies from $requirementsName..."
        Write-Host 'This can download several gigabytes on the first run.'
        Run-Checked $venvPython @('-m', 'pip', 'install', '--no-cache-dir', '-r', $requirementsPath)

        Write-Host 'Checking installed libraries...'
        $check = 'import PIL,cryptography,llama_cpp,numpy,psutil,pyaudio,torch,torchaudio,torchvision,whisper,win32com.client'
        if ($Backend -eq 'cu124') {
            $check += ',pynvml'
        }
        Run-Checked $venvPython @('-c', $check)

        $newState = [ordered]@{
            backend = $Backend
            requirements = $requirementsName
            sha256 = $requirementsHash
            python = '3.12-x64'
        }
        $newState | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    } else {
        Write-Host '[3/4] Dependencies are already prepared.'
    }

    Write-Host '[4/4] Starting LLM Local Chat...'
    Write-Host 'Keep this window open while the application is running.'
    Run-Checked $venvPython @($appFile)
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Retry the same batch after correcting the error.'
    Write-Host 'To force CPU mode: Easy_LLM_Local_Chat.bat -Backend cpu'
    Write-Host 'To force NVIDIA CUDA 12.4 mode: Easy_LLM_Local_Chat.bat -Backend cu124'
    exit 1
}
