param(
    [string]$LibMpvDll = "",
    [string]$Destination = "",
    [string]$SourceStage = "",
    [switch]$Clean,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $repo
$python = Join-Path $workspace ".cache\windows-build\.venv\Scripts\python.exe"
$buildRoot = Join-Path $workspace ("tmp\windows-build-" + [guid]::NewGuid().ToString("N"))
$stageRoot = Join-Path $buildRoot "source"
$sourceDir = Join-Path $buildRoot "dist\BS Podcasts"
$sourceExe = Join-Path $sourceDir "BS Podcasts.exe"
if (-not $Destination) { $Destination = Join-Path $workspace "dists\windows" }

if (-not $LibMpvDll) {
    # Default to the workspace-built LGPL libmpv (mpv -Dgpl=false + LGPL FFmpeg),
    # See the complete third-party inventory before distribution.
    $LibMpvDll = Join-Path $workspace ".cache\windows-build\libmpv-lgpl\libmpv-2.dll"
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Windows build environment is missing: $python`nSee the project docs for approved dependency setup."
}
if (-not (Test-Path -LiteralPath $LibMpvDll)) {
    throw "Cached libmpv DLL is missing: $LibMpvDll. Supply -LibMpvDll explicitly."
}

$env:BS_PODCASTS_LIBMPV_DLL = (Resolve-Path -LiteralPath $LibMpvDll).ProviderPath
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
$env:TEMP = $buildRoot
$env:TMP = $buildRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $workspace ".cache\pyinstaller-windows"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
if ($SourceStage) {
    $stageRoot = (Resolve-Path -LiteralPath $SourceStage).ProviderPath
    $originPath = Join-Path $stageRoot ".build-origin.json"
    if (-not (Test-Path -LiteralPath $originPath)) { throw "Source stage has no origin record" }
    $origin = Get-Content -LiteralPath $originPath -Raw | ConvertFrom-Json
    $digest = & $python -B (Join-Path $stageRoot "packaging\source_manifest.py") hash
    if ($LASTEXITCODE -ne 0 -or $digest.Trim() -ne $origin.sha256) { throw "Source stage fingerprint mismatch" }
} else {
    & $python -B (Join-Path $repo "packaging\source_manifest.py") stage --destination $stageRoot
    if ($LASTEXITCODE -ne 0) { throw "Source staging failed" }
}
& $python -B (Join-Path $stageRoot "packaging\check_dependencies.py") (Join-Path $stageRoot "packaging\requirements-windows.lock")
if ($LASTEXITCODE -ne 0) { throw "Build environment failed the approved dependency check" }
$nativeReceiptPath = Join-Path (Split-Path -Parent $env:BS_PODCASTS_LIBMPV_DLL) "native-build.json"
if (-not (Test-Path -LiteralPath $nativeReceiptPath)) { throw "Native input receipt is missing; rebuild the pinned native dependencies" }
$nativeReceipt = Get-Content -LiteralPath $nativeReceiptPath -Raw | ConvertFrom-Json
$nativeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $env:BS_PODCASTS_LIBMPV_DLL).Hash.ToLowerInvariant()
$nativeLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stageRoot "packaging\native-sources.json")).Hash.ToLowerInvariant()
if ($nativeReceipt.sha256 -ne $nativeHash -or $nativeReceipt.inputs_sha256 -ne $nativeLockHash) {
    throw "Native DLL or dependency inputs differ from the recorded build"
}
$env:BS_PODCASTS_NATIVE_RECEIPT = $nativeReceiptPath
$spec = Join-Path $stageRoot "packaging\bs-podcasts.spec"
$env:BS_PODCASTS_SOURCE_STAGE = $stageRoot
$env:BS_PODCASTS_BUILD_WORK = Join-Path $buildRoot "work"
Push-Location $buildRoot
try {
    $arguments = @("-B", "-m", "PyInstaller", "--noconfirm", "--distpath", (Join-Path $buildRoot "dist"), "--workpath", $env:BS_PODCASTS_BUILD_WORK)
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

# Execute only the non-GUI diagnostic in a disposable profile before publication.
$checkDir = Join-Path $buildRoot "self-check"
$checkArgs = '--self-check --self-check-dir "' + $checkDir + '"'
$checkStart = New-Object System.Diagnostics.ProcessStartInfo
$checkStart.FileName = $sourceExe
$checkStart.Arguments = $checkArgs
$checkStart.WorkingDirectory = $buildRoot
$checkStart.UseShellExecute = $false
$checkStart.CreateNoWindow = $true
$checkProcess = [System.Diagnostics.Process]::Start($checkStart)
if (-not $checkProcess.WaitForExit(30000)) {
    $checkProcess.Kill()  # Only the owned diagnostic process, never another app.
    throw "Packaged self-check timed out; previous application unchanged"
}
$checkReport = Join-Path $checkDir "self-check.json"
if ($checkProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $checkReport)) {
    throw "Packaged self-check failed; previous application unchanged"
}
$checked = Get-Content -LiteralPath $checkReport -Raw | ConvertFrom-Json
if (-not $checked.passed) { throw "Packaged dependency/storage/asset check failed; previous application unchanged" }
Write-Output "Packaged self-check passed (no GUI, real profile or audio device)."

# Stage a complete replacement and keep the previous app recoverable.
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$copiedDir = Join-Path $Destination "BS Podcasts"
$bsStage = Join-Path $Destination (".BS-Podcasts-incoming-" + [guid]::NewGuid().ToString("N"))
$retiredDir = Join-Path $Destination ("BS Podcasts.previous-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
Copy-Item -LiteralPath $sourceDir -Destination $bsStage -Recurse -ErrorAction Stop
$bsRunning = Get-Process -Name "BS Podcasts" -ErrorAction SilentlyContinue
if ($bsRunning) {
    throw "Close BS Podcasts before replacing it. New build is staged at $bsStage; the previous copy is unchanged."
}
if (Test-Path -LiteralPath $copiedDir) {
    Move-Item -LiteralPath $copiedDir -Destination $retiredDir -ErrorAction Stop
}
try {
    Move-Item -LiteralPath $bsStage -Destination $copiedDir -ErrorAction Stop
}
catch {
    if ((Test-Path -LiteralPath $retiredDir) -and -not (Test-Path -LiteralPath $copiedDir)) {
        Move-Item -LiteralPath $retiredDir -Destination $copiedDir
    }
    throw
}
if (Test-Path -LiteralPath $retiredDir) {
    Write-Output "Previous application retained: $retiredDir"
}

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

$manifest = Join-Path $Destination "BS-Podcasts-Windows.manifest.json"
& $python -B (Join-Path $stageRoot "packaging\build_manifest.py") --artifact $copiedDir --output $manifest
if ($LASTEXITCODE -ne 0) { throw "Artifact manifest generation failed" }

if ($Installer) {
    $iscc = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $iscc) {
        throw "-Installer was requested, but Inno Setup 6 was not found. The application was still copied to $copiedDir"
    }
    $installerSpec = Join-Path $stageRoot "packaging\bs-podcasts.iss"
    # One source of truth for the version. The .iss used to carry its own
    # literal, so the installer advertised a stale version the moment
    # pyproject.toml was bumped without it.
    $pyproject = Get-Content -LiteralPath (Join-Path $stageRoot "pyproject.toml") -Raw
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
