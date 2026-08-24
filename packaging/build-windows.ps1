param(
    [string]$LibMpvDll = "",
    [string]$Destination = ".\dists\windows",
    [switch]$Clean,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $repo
$python = Join-Path $repo ".venv-win\Scripts\python.exe"
$spec = Join-Path $repo "packaging\bs-podcasts.spec"
$sourceExe = Join-Path $repo "dist\BS Podcasts.exe"

if (-not $LibMpvDll) {
    $LibMpvDll = Join-Path $workspace "tmp\windows-build\libmpv\libmpv-2.dll"
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Windows build environment is missing: $python`nSee WINDOWS-BUILD.md for the one-time setup."
}
if (-not (Test-Path -LiteralPath $LibMpvDll)) {
    throw "Cached libmpv DLL is missing: $LibMpvDll`nSee WINDOWS-BUILD.md for the restore command."
}

$env:BS_PODCASTS_LIBMPV_DLL = (Resolve-Path -LiteralPath $LibMpvDll).ProviderPath
Push-Location $repo
try {
    $arguments = @("-m", "PyInstaller", "--noconfirm")
    if ($Clean) {
        $arguments += "--clean"
    }
    $arguments += $spec
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$copiedExe = Join-Path $Destination "BS Podcasts.exe"
Copy-Item -LiteralPath $sourceExe -Destination $copiedExe -Force

$item = Get-Item -LiteralPath $copiedExe
$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $copiedExe
Write-Output "Copied binary: $($item.FullName)"
Write-Output "Size: $($item.Length) bytes"
Write-Output "SHA-256: $($hash.Hash)"

if ($Installer) {
    $iscc = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $iscc) {
        throw "-Installer was requested, but Inno Setup 6 was not found. The binary was still copied to $copiedExe"
    }
    $installerSpec = Join-Path $repo "packaging\bs-podcasts.iss"
    & $iscc "/DSourceExe=$copiedExe" "/DOutputDir=$Destination" $installerSpec
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }
}
