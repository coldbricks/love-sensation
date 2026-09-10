[CmdletBinding()]
param(
    # Optional executable path for a fresh environment; an existing .venv is retained.
    [string]$Python,
    [switch]$Dev,
    # Dependency-only setup: defer the model download and device test until first use.
    [switch]$SkipDiagnose
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') {
    throw 'Setup.ps1 installs the Windows desktop app. Use the test workflow for other platforms.'
}
$platinumRoot = $PSScriptRoot
$platinumVenv = Join-Path $platinumRoot '.venv'
$platinumRuntime = Join-Path $platinumVenv 'Scripts\python.exe'

function Find-PlatinumPython {
    param([string]$Executable, [string[]]$Prefix = @())
    $platinumProbe = @'
import struct, sys
valid = (3, 12) <= sys.version_info[:2] < (3, 15) and struct.calcsize('P') == 8
print(sys.executable if valid else '')
sys.exit(0 if valid else 1)
'@
    try {
        $platinumFound = & $Executable @Prefix -c $platinumProbe 2>$null
        if ($LASTEXITCODE -eq 0 -and $platinumFound) {
            return ([string]$platinumFound).Trim()
        }
    }
    catch {
        # A missing launcher target is expected while trying installed versions.
    }
    return $null
}

if (Test-Path -LiteralPath $platinumRuntime -PathType Leaf) {
    if (-not (Find-PlatinumPython -Executable $platinumRuntime)) {
        throw 'The existing .venv must use 64-bit Python 3.12, 3.13, or 3.14. It was left unchanged.'
    }
    Write-Host 'Keeping the existing .venv and its interpreter.'
}
else {
    if (Test-Path -LiteralPath $platinumVenv) {
        throw 'An incomplete .venv already exists. Rename it before retrying; setup will not delete it.'
    }
    $platinumPython = $null
    if ($Python) {
        $platinumPython = Find-PlatinumPython -Executable $Python
        if (-not $platinumPython) {
            throw 'The -Python executable must be a working 64-bit Python 3.12, 3.13, or 3.14.'
        }
    }
    else {
        $platinumLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($platinumLauncher) {
            foreach ($platinumVersion in @('-3.12', '-3.13', '-3.14')) {
                $platinumPython = Find-PlatinumPython -Executable $platinumLauncher.Source -Prefix @($platinumVersion)
                if ($platinumPython) { break }
            }
        }
        if (-not $platinumPython) {
            $platinumCommand = Get-Command python -ErrorAction SilentlyContinue
            if ($platinumCommand) {
                $platinumPython = Find-PlatinumPython -Executable $platinumCommand.Source
            }
        }
    }
    if (-not $platinumPython) {
        throw 'Install 64-bit Python 3.12-3.14 from python.org, then retry or supply -Python with its executable path.'
    }
    Write-Host "Creating an isolated environment with $platinumPython"
    & $platinumPython -m venv $platinumVenv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}

# Keep an already compatible installation, including inherited CUDA packages in
# an older environment. Fresh installs use a matched official CUDA 12.8 pair.
$platinumTorchProbe = @'
import sys
import torch, torchvision
from torchvision.ops import nms
nms(torch.tensor([[0., 0., 1., 1.]]), torch.tensor([1.]), 0.5)
sys.exit(0 if torch.version.cuda == '12.8' else 1)
'@
$platinumKeepTorch = $false
try {
    & $platinumRuntime -c $platinumTorchProbe 2>$null
    $platinumKeepTorch = $LASTEXITCODE -eq 0
}
catch {
    $platinumKeepTorch = $false
}
if ($platinumKeepTorch) {
    Write-Host 'Keeping the existing compatible PyTorch / torchvision CUDA 12.8 installation.'
}
else {
    # Version pair: https://pytorch.org/get-started/previous-versions/
    & $platinumRuntime -m pip install --upgrade 'torch==2.10.0' 'torchvision==0.25.0' --index-url 'https://download.pytorch.org/whl/cu128'
    if ($LASTEXITCODE -ne 0) { throw 'CUDA 12.8 PyTorch installation failed. See the pip output above.' }
}

& $platinumRuntime -m pip install -r (Join-Path $platinumRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'App dependency installation failed.' }
if ($Dev) {
    & $platinumRuntime -m pip install -r (Join-Path $platinumRoot 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Test dependency installation failed.' }
}

$platinumDeviceProbe = @'
import json, torch, torchvision
available = torch.cuda.is_available()
print(json.dumps({'torch': torch.__version__, 'torchvision': torchvision.__version__,
                  'cuda_build': torch.version.cuda, 'cuda_available': available,
                  'device': torch.cuda.get_device_name(0) if available else 'CPU (CUDA unavailable)'}))
if torch.version.cuda != '12.8':
    raise RuntimeError('Setup expected a CUDA 12.8 PyTorch build.')
'@
& $platinumRuntime -c $platinumDeviceProbe
if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency verification failed.' }
if (-not $SkipDiagnose) {
    Write-Host 'Verifying the model and compute device. The first run downloads the detector weights.'
    & $platinumRuntime (Join-Path $platinumRoot 'main.py') --diagnose
    if ($LASTEXITCODE -ne 0) { throw 'Model/device verification failed. See the output above.' }
}
else {
    Write-Host 'Dependencies installed. Model/device diagnosis was skipped; the model downloads on first analysis.'
}
Write-Host 'Ready. Double-click Launch Love Sensation.vbs.'
