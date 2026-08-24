[CmdletBinding()]
param(
    [string]$Python = $(if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" })
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$BuildRoot = if ($env:BUILD_ROOT) {
    [System.IO.Path]::GetFullPath($env:BUILD_ROOT)
} else {
    Join-Path $RootDir ".build\windows"
}
$Venv = Join-Path $BuildRoot "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Python 3.12 is required and '$Python' was not found on PATH."
}

New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Invoke-Native -FilePath $Python -Arguments @("-m", "venv", $Venv)
}

$VersionOutput = & $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$VersionExitCode = $LASTEXITCODE
$Version = ($VersionOutput | Out-String).Trim()
if ($VersionExitCode -ne 0 -or $Version -ne "3.12") {
    throw "The packaging environment must use Python 3.12; found '$Version'."
}

Invoke-Native -FilePath $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
Invoke-Native -FilePath $VenvPython -Arguments @("-m", "pip", "install", "-e", ".", "pyinstaller")

$BuildDir = Join-Path $RootDir "build"
$DistDir = Join-Path $RootDir "dist"
if (Test-Path -LiteralPath $BuildDir) {
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}
if (Test-Path -LiteralPath $DistDir) {
    Remove-Item -LiteralPath $DistDir -Recurse -Force
}

$CommonArgs = @(
    "--noconfirm"
    "--clean"
    "--onedir"
    "--collect-all", "imageio_ffmpeg"
    "--collect-all", "PyOpenColorIO"
    "--collect-all", "cv2"
    "--paths", $RootDir
)

$GuiArgs = $CommonArgs + @(
    "--windowed"
    "--name", "VP Stitch"
    "--add-data", "configs;configs"
    "packaging/gui_entry.py"
)
Invoke-Native -FilePath $PyInstaller -Arguments $GuiArgs

$CliArgs = $CommonArgs + @(
    "--console"
    "--name", "vpstitch-cli"
    "packaging/cli_entry.py"
)
Invoke-Native -FilePath $PyInstaller -Arguments $CliArgs

$GuiDir = Join-Path $DistDir "VP Stitch"
$GuiInternal = Join-Path $GuiDir "_internal"
$CliDir = Join-Path $DistDir "vpstitch-cli"
$CliExe = Join-Path $CliDir "vpstitch-cli.exe"
$CliInternal = Join-Path $CliDir "_internal"

if (-not (Test-Path -LiteralPath $CliExe -PathType Leaf)) {
    throw "PyInstaller CLI output was not found: $CliExe"
}

if (Test-Path -LiteralPath $CliInternal -PathType Container) {
    New-Item -ItemType Directory -Path $GuiInternal -Force | Out-Null
    Copy-Item -Path (Join-Path $CliInternal "*") -Destination $GuiInternal -Recurse -Force
}

# The frozen GUI resolves the platform-specific .exe helper beside itself.
Copy-Item -LiteralPath $CliExe -Destination (Join-Path $GuiDir "vpstitch-cli.exe") -Force

$Architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
$Archive = Join-Path $DistDir "VP-Stitch-Windows-$Architecture.zip"
if (Test-Path -LiteralPath $Archive) {
    Remove-Item -LiteralPath $Archive -Force
}
Compress-Archive -Path $GuiDir -DestinationPath $Archive -CompressionLevel Optimal

Write-Host "Built: $GuiDir"
Write-Host "Archive: $Archive"
