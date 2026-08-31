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
$sourceDir = Join-Path $repo "dist\BS Podcasts"
$sourceExe = Join-Path $sourceDir "BS Podcasts.exe"

if (-not $LibMpvDll) {
    # Default to the workspace-built LGPL libmpv (mpv -Dgpl=false + LGPL FFmpeg),
    # required for binary-only distribution. See WINDOWS-BUILD.md to rebuild it.
    $LibMpvDll = Join-Path $workspace "tmp\windows-build\libmpv-lgpl\libmpv-2.dll"
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

if (-not (Test-Path -LiteralPath $sourceExe)) {
    throw "Build finished but the application folder is missing: $sourceExe"
}

# The application is a onedir folder; replace the previous copy wholesale so
# removed DLLs do not linger. Renaming first fails fast and harmlessly while
# the app is running (locked files), instead of half-deleting the old copy.
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$copiedDir = Join-Path $Destination "BS Podcasts"
$retiredDir = Join-Path $Destination "BS Podcasts.old"
if (Test-Path -LiteralPath $retiredDir) {
    Remove-Item -LiteralPath $retiredDir -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $copiedDir) {
    try {
        Rename-Item -LiteralPath $copiedDir -NewName "BS Podcasts.old" -ErrorAction Stop
    }
    catch {
        throw "The previous copy at $copiedDir is in use (is BS Podcasts running?). Close it and re-run; nothing was changed."
    }
}
Copy-Item -LiteralPath $sourceDir -Destination $copiedDir -Recurse -Force
Remove-Item -LiteralPath $retiredDir -Recurse -Force -ErrorAction SilentlyContinue

# Surface the third-party notices at the folder root where users can find them.
$notices = Join-Path $copiedDir "_internal\THIRD-PARTY-NOTICES.txt"
if (Test-Path -LiteralPath $notices) {
    Copy-Item -LiteralPath $notices -Destination (Join-Path $copiedDir "THIRD-PARTY-NOTICES.txt") -Force
}

$copiedExe = Join-Path $copiedDir "BS Podcasts.exe"
$size = (Get-ChildItem -LiteralPath $copiedDir -Recurse -File | Measure-Object -Sum Length).Sum
$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $copiedExe
Write-Output "Copied application: $copiedDir"
Write-Output "Launcher: $copiedExe"
Write-Output "Total size: $size bytes"
Write-Output "Launcher SHA-256: $($hash.Hash)"

if ($Installer) {
    $iscc = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $iscc) {
        throw "-Installer was requested, but Inno Setup 6 was not found. The application was still copied to $copiedDir"
    }
    $installerSpec = Join-Path $repo "packaging\bs-podcasts.iss"
    # One source of truth for the version. The .iss used to carry its own
    # literal, so the installer advertised a stale version the moment
    # pyproject.toml was bumped without it.
    $pyproject = Get-Content -LiteralPath (Join-Path $repo "pyproject.toml") -Raw
    if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
        throw "Could not read version from pyproject.toml"
    }
    $appVersion = $Matches[1]
    Write-Output "Installer version: $appVersion"
    & $iscc "/DSourceDir=$copiedDir" "/DOutputDir=$Destination" "/DMyAppVersion=$appVersion" $installerSpec
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }
}
