param(
    [string]$LibMpvDll = "",
    [string]$Destination = "",
    [string]$SourceStage = "",
    [string]$CheckRoot = "",
    [switch]$Clean,
    [switch]$Installer,
    [switch]$PruneOldDeployments,
    [switch]$Plain
)

$ErrorActionPreference = "Stop"
$buildTimer = [System.Diagnostics.Stopwatch]::StartNew()
# Colour only where ANSI is rendered: the WSL terminal pipe or Windows Terminal.
# -Plain or NO_COLOR keeps plain text, for example when the output goes to a log.
$useColour = -not $Plain -and -not $env:NO_COLOR -and ([Console]::IsOutputRedirected -or $env:WT_SESSION)

function Format-Colour([string]$Code, [string]$Text) {
    if ($script:useColour -and $Code) { return "$([char]27)[${Code}m$Text$([char]27)[0m" }
    return $Text
}

function Write-State {
    param([ValidateSet("ok", "warn", "info")][string]$Level, [string]$Text)
    $label, $code = @{ ok = @("OK  ", "32"); warn = @("WARN", "33"); info = @("    ", "") }[$Level]
    Write-Output ((Format-Colour $code $label) + " " + $Text)
}

function Write-Stats {
    param([System.Collections.Specialized.OrderedDictionary]$Rows)
    $Rows["elapsed"] = "{0:N1} s" -f $script:buildTimer.Elapsed.TotalSeconds
    $width = ($Rows.Keys | ForEach-Object { $_.Length } | Measure-Object -Maximum).Maximum
    Write-Output (Format-Colour "1" "Stats")
    foreach ($key in $Rows.Keys) {
        Write-Output ("  " + $key.PadRight($width) + "  " + $Rows[$key])
    }
}

function Format-Size([long]$Bytes) {
    return "{0:N1} MB ({1:N0} bytes)" -f ($Bytes / 1MB), $Bytes
}

function Remove-OldDeployments {
    param([string]$Root, [string]$Retain, [int]$Keep = 3)
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $retainPath = [System.IO.Path]::GetFullPath($Retain)
    # Only this script's own .previous-<stamp>-<guid> rollbacks. The shared folder
    # also holds other platforms' rollbacks and in-flight staging; never prune those.
    $dirs = @(Get-ChildItem -LiteralPath $Root -Directory -Force | Where-Object {
        $_.Name -cmatch '^\.previous-\d{8}-\d{6}-[0-9a-f]{32}$' -and
        -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -and
        [System.IO.Path]::GetFullPath($_.FullName) -ne $retainPath
    } | Sort-Object LastWriteTime -Descending)
    $leave = [Math]::Max($Keep - 1, 0)
    if ($dirs.Count -le $leave) { return }
    foreach ($dir in $dirs[$leave..($dirs.Count - 1)]) {
        Remove-Item -LiteralPath $dir.FullName -Recurse -Force
    }
}

function Remove-BuildRoot {
    param([string]$Root, [string]$Workspace)
    # Only this run's own tmp\windows-build-<guid> work folder, never a link or another path.
    $full = [System.IO.Path]::GetFullPath($Root)
    $tmpRoot = [System.IO.Path]::GetFullPath((Join-Path $Workspace "tmp"))
    if (-not [string]::Equals([System.IO.Path]::GetDirectoryName($full), $tmpRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetFileName($full) -cnotmatch '^windows-build-[0-9a-f]{32}$') { return }
    if (-not (Test-Path -LiteralPath $full)) { return }
    if ((Get-Item -LiteralPath $full -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return }
    try {
        Remove-Item -LiteralPath $full -Recurse -Force
    } catch {
        Write-Warning "Could not remove build work folder ${full}: $($_.Exception.Message)"
    }
}

function Publish-Application {
    param([string]$Application, [string]$Manifest, [string]$Target, [string]$MetadataRoot,
          [string]$InstallerPath = "", [switch]$PruneOldDeployments)
    New-Item -ItemType Directory -Path $MetadataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    $lease = [System.IO.File]::Open((Join-Path $MetadataRoot ".publish.lock"), "OpenOrCreate", "ReadWrite", "None")
    $token = [guid]::NewGuid().ToString("N")
    $incoming = Join-Path $MetadataRoot (".incoming-" + $token)
    $previous = Join-Path $MetadataRoot (".previous-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $token)
    $names = @("BS Podcasts", "BS-Podcasts-Windows.manifest.json")
    $sources = @($Application, $Manifest)
    if ($InstallerPath) {
        $names += [System.IO.Path]::GetFileName($InstallerPath)
        $sources += $InstallerPath
    }
    $retired = @()
    $published = @()
    try {
        New-Item -ItemType Directory -Path $incoming | Out-Null
        for ($i = 0; $i -lt $names.Count; $i++) {
            Move-Item -LiteralPath $sources[$i] -Destination (Join-Path $incoming $names[$i])
        }
        $applicationPath = (Join-Path $Target "BS Podcasts") + "\"
        foreach ($process in (Get-Process -Name "BS Podcasts" -ErrorAction SilentlyContinue)) {
            if (-not $process.Path -or $process.Path.StartsWith($applicationPath, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Close this copy of BS Podcasts before replacing it. The verified new build is retained at $incoming."
            }
        }
        New-Item -ItemType Directory -Path $previous | Out-Null
        try {
            foreach ($name in $names) {
                $current = Join-Path $Target $name
                if (Test-Path -LiteralPath $current) {
                    if ((Get-Item -LiteralPath $current).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                        throw "Refusing to replace an artifact link: $name"
                    }
                    Move-Item -LiteralPath $current -Destination (Join-Path $previous $name)
                    $retired += $name
                }
            }
            foreach ($name in $names) {
                Move-Item -LiteralPath (Join-Path $incoming $name) -Destination (Join-Path $Target $name)
                $published += $name
            }
        } catch {
            [array]::Reverse($published)
            foreach ($name in $published) {
                Move-Item -LiteralPath (Join-Path $Target $name) -Destination (Join-Path $incoming $name)
            }
            [array]::Reverse($retired)
            foreach ($name in $retired) {
                Move-Item -LiteralPath (Join-Path $previous $name) -Destination (Join-Path $Target $name)
            }
            throw
        }
        if (Test-Path -LiteralPath $incoming) {
            Remove-Item -LiteralPath $incoming -Recurse -Force
        }
        if ($PruneOldDeployments) {
            Remove-OldDeployments -Root $MetadataRoot -Retain $previous
        }
        Write-State ok "Previous application and manifest retained: $previous"
    } finally {
        $lease.Dispose()
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $repo
$python = Join-Path $workspace ".cache\windows-build\.venv\Scripts\python.exe"
$buildRoot = Join-Path $workspace ("tmp\windows-build-" + [guid]::NewGuid().ToString("N"))
$stageRoot = Join-Path $buildRoot "source"
$sourceDir = Join-Path $buildRoot "dist\BS Podcasts"
$sourceExe = Join-Path $sourceDir "BS Podcasts.exe"
if (-not $Destination) { $Destination = Join-Path $workspace "dists\windows" }
# The build publishes its manifest and installer beside the app, so it stays inside
# this workspace's dists folder. The owner's Windows app copy receives the runtime
# files alone through the separate app-only delivery step, never through this recipe.
$Destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Destination)
$distsRoot = [System.IO.Path]::GetFullPath((Join-Path $workspace "dists")).TrimEnd('\') + '\'
if (-not ([System.IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\').StartsWith($distsRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "-Destination must be inside $distsRoot; copy the app anywhere else with the app-only delivery step."
}
if (-not $CheckRoot) {
    $privateBuild = Join-Path $workspace ".config\build.json"
    if (Test-Path -LiteralPath $privateBuild) {
        $configured = Get-Content -LiteralPath $privateBuild -Raw | ConvertFrom-Json
        if ($configured.windows_check_root) {
            $CheckRoot = [string]$configured.windows_check_root
        }
    }
}
if (-not $CheckRoot -or $CheckRoot -notmatch '^[A-Za-z]:[\\/]') {
    throw "Set an explicitly approved -CheckRoot or private windows_check_root on a local Windows drive; it is never inferred from the app-copy destination."
}

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
# Remove this run's build work on success; keep it after a failure for diagnosis.
$buildSucceeded = $false
try {
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
    # One source of truth for the version. The packaged self-check must report it
    # before anything is published, and the installer reuses it.
    $pyproject = Get-Content -LiteralPath (Join-Path $stageRoot "pyproject.toml") -Raw
    if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
        throw "Could not read version from pyproject.toml"
    }
    $projectVersion = $Matches[1]
    $nativeReceiptPath = Join-Path (Split-Path -Parent $env:BS_PODCASTS_LIBMPV_DLL) "native-build.json"
    if (-not (Test-Path -LiteralPath $nativeReceiptPath)) { throw "Native input receipt is missing; rebuild the pinned native dependencies" }
    $nativeReceipt = Get-Content -LiteralPath $nativeReceiptPath -Raw | ConvertFrom-Json
    $nativeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $env:BS_PODCASTS_LIBMPV_DLL).Hash.ToLowerInvariant()
    $nativeLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stageRoot "packaging\native-sources.json")).Hash.ToLowerInvariant()
    $nativeRecipeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stageRoot "packaging\build-libmpv-lgpl.sh")).Hash.ToLowerInvariant()
    if ($nativeReceipt.sha256 -ne $nativeHash -or $nativeReceipt.inputs_sha256 -ne $nativeLockHash -or $nativeReceipt.recipe_sha256 -ne $nativeRecipeHash) {
        throw "Native DLL, inputs or recipe differ from the recorded build; rebuild the native library. Legacy receipts cannot be relabeled as verified."
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
    $checkDir = Join-Path $CheckRoot ("build-self-check-" + [guid]::NewGuid().ToString("N"))
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
    if ([string]$checked.version -cne $projectVersion) {
        throw "Packaged version '$($checked.version)' differs from pyproject.toml '$projectVersion'; previous application unchanged"
    }
    Write-State ok "Packaged self-check passed for version $projectVersion (no GUI, real profile or audio device)."

    # Complete notices and provenance before replacing any previous artifact.
    & $python -B (Join-Path $stageRoot "packaging\check_frozen.py") $sourceDir --private-root $workspace
    if ($LASTEXITCODE -ne 0) { throw "Frozen privacy check failed; previous application unchanged" }
    $notices = Join-Path $sourceDir "_internal\THIRD-PARTY-NOTICES.txt"
    if (-not (Test-Path -LiteralPath $notices -PathType Leaf)) { throw "Required bundled notices are missing; previous application unchanged" }
    Copy-Item -LiteralPath $notices -Destination (Join-Path $sourceDir "THIRD-PARTY-NOTICES.txt")
    $preparedManifest = Join-Path $buildRoot "BS-Podcasts-Windows.manifest.json"
    & $python -B (Join-Path $stageRoot "packaging\build_manifest.py") --artifact $sourceDir --output $preparedManifest
    if ($LASTEXITCODE -ne 0) { throw "Artifact manifest generation failed; previous application unchanged" }
    $reportLog = Join-Path $workspace ("logs\windows-package-self-check-" + [System.IO.Path]::GetFileName($checkDir) + ".json")
    New-Item -ItemType Directory -Path (Join-Path $workspace "logs") -Force | Out-Null
    Copy-Item -LiteralPath $checkReport -Destination $reportLog
    $setup = ""
    if ($Installer) {
        $iscc = @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
        if (-not $iscc) { throw "-Installer requested, but Inno Setup 6 was not found; previous artifacts unchanged" }
        $installerDir = Join-Path $buildRoot "installer"
        $installerSpec = Join-Path $stageRoot "packaging\bs-podcasts.iss"
        Write-State info "Preparing installer for version $projectVersion before publication"
        & $iscc "/DSourceDir=$sourceDir" "/DOutputDir=$installerDir" "/DMyAppVersion=$projectVersion" $installerSpec
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE; previous artifacts unchanged" }
        $setup = Join-Path $installerDir "BS-Podcasts-$projectVersion-Setup.exe"
        if (-not (Test-Path -LiteralPath $setup -PathType Leaf) -or (Get-Item -LiteralPath $setup).Length -le 0) {
            throw "Inno Setup produced no non-empty installer; previous artifacts unchanged"
        }
        $manifestRecord = Get-Content -LiteralPath $preparedManifest -Raw | ConvertFrom-Json
        $manifestRecord | Add-Member -NotePropertyName installer -NotePropertyValue ([ordered]@{
            path = [System.IO.Path]::GetFileName($setup)
            bytes = (Get-Item -LiteralPath $setup).Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLowerInvariant()
        })
        [System.IO.File]::WriteAllText($preparedManifest, ($manifestRecord | ConvertTo-Json -Depth 100), (New-Object System.Text.UTF8Encoding($false)))
    }
    $metadataRoot = Join-Path $workspace ".cache\previous-deployments"
    Publish-Application -Application $sourceDir -Manifest $preparedManifest -Target $Destination -MetadataRoot $metadataRoot -InstallerPath $setup -PruneOldDeployments:$PruneOldDeployments
    $copiedDir = Join-Path $Destination "BS Podcasts"

    $copiedExe = Join-Path $copiedDir "BS Podcasts.exe"
    $copiedFiles = @(Get-ChildItem -LiteralPath $copiedDir -Recurse -File)
    $size = ($copiedFiles | Measure-Object -Sum Length).Sum
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $copiedExe
    Write-State ok "Published BS Podcasts ${projectVersion}: $copiedDir"
    $stats = [ordered]@{
        "version" = $projectVersion
        "app files" = $copiedFiles.Count
        "app size" = Format-Size $size
        "launcher" = $copiedExe
        "launcher SHA-256" = $hash.Hash
        "rollbacks pruned" = $(if ($PruneOldDeployments) { "older than the newest 3" } else { "none (opt-in)" })
    }

    if ($setup) {
        $publishedSetup = Join-Path $Destination ([System.IO.Path]::GetFileName($setup))
        $stats["installer"] = "$publishedSetup, " + (Format-Size (Get-Item -LiteralPath $publishedSetup).Length)
    }
    Write-Stats $stats
    $buildSucceeded = $true
    # Preserve the report on the build host, then clean only this run's approved
    # disposable profile. Failed profiles remain available for diagnosis.
    if ((Get-Item -LiteralPath $checkDir).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Write-Warning "Diagnostic profile became a link; retained for inspection: $checkDir"
    } else {
        try { Remove-Item -LiteralPath $checkDir -Recurse -Force }
        catch { Write-Warning "Disposable diagnostic profile could not be removed: $checkDir" }
    }
} finally {
    if ($buildSucceeded) {
        Remove-BuildRoot -Root $buildRoot -Workspace $workspace
    } elseif (Test-Path -LiteralPath $buildRoot) {
        Write-Output "Build work kept for diagnosis: $buildRoot"
    }
}
