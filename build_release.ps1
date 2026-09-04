<#
.SYNOPSIS
    End-to-end release pipeline for PDF to Data.

.DESCRIPTION
    1) PyInstaller  -> dist\PDFtoData.exe
    2) Assemble the portable bundle:
         dist\bundle\PDFtoData.exe
         dist\bundle\runtime\python\   embedded CPython + opendataloader-pdf[hybrid]
         dist\bundle\runtime\jre\      portable Temurin JRE 17
         dist\bundle\version.json
         dist\bundle\update_helper.ps1
    3) Zip           -> dist\PDFtoData-<ver>-portable.zip  (+ prints its SHA-256)
    4) Inno Setup    -> dist\PDFtoData-Setup-<ver>.exe
    5) Optional      -> gh release create v<ver> ... --draft

.PARAMETER Version
    Release version, e.g. 1.2.0. Required.

.PARAMETER ChangelogFile
    Markdown notes passed to `gh release create --notes-file`.

.PARAMETER SkipRelease
    Build everything but do not touch GitHub.

.PARAMETER NoHybrid
    Install plain opendataloader-pdf instead of [hybrid]. The hybrid extra pulls
    torch/docling/easyocr and adds roughly a gigabyte to the bundle; use this for
    a slim build whose users only need Local mode.

.EXAMPLE
    .\build_release.ps1 -Version 1.0.0 -ChangelogFile notes.md
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$ChangelogFile,
    [switch]$SkipRelease,
    [switch]$NoHybrid,
    [string]$Repo = "YOUR-GITHUB-USERNAME/PDFtoData",
    [string]$PythonEmbedUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip",
    [string]$JreUrl = "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk"
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # much faster Invoke-WebRequest downloads

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Dist      = Join-Path $Root 'dist'
$Bundle    = Join-Path $Dist 'bundle'
$Runtime   = Join-Path $Bundle 'runtime'
$PyDir     = Join-Path $Runtime 'python'
$JreDir    = Join-Path $Runtime 'jre'
$Work      = Join-Path $Dist '_work'
$AppSlug   = 'PDFtoData'

function Say  { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Note { param([string]$m) Write-Host "    $m" -ForegroundColor DarkGray }
function Die  { param([string]$m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

if ($Version -notmatch '^\d+\.\d+\.\d+') { Die "Version must look like 1.2.3 (got '$Version')" }

Say "Building $AppSlug $Version"
New-Item -ItemType Directory -Force -Path $Dist, $Work | Out-Null

# ---------------------------------------------------------------- 1) PyInstaller
Say '1/5  PyInstaller'
$pyinstaller = (Get-Command pyinstaller -ErrorAction SilentlyContinue)
if (-not $pyinstaller) { Die "pyinstaller not found. Run: pip install pyinstaller" }

$piArgs = @('--onefile', '--windowed', '--name', $AppSlug, '--noconfirm',
            '--distpath', $Dist, '--workpath', (Join-Path $Work 'build'),
            '--specpath', $Work)
$icon = Join-Path $Root 'assets\icon.ico'
if (Test-Path $icon) { $piArgs += @('--icon', $icon); Note "using icon $icon" }
# updater.py is imported by app.py; add it explicitly so onefile definitely has it.
$piArgs += @('--hidden-import', 'updater')
# NOTE: never redirect a native command's stderr directly in PowerShell 5.1 -
# it wraps each line in a NativeCommandError, which $ErrorActionPreference='Stop'
# turns into a terminating error even when the exe exited 0. Route through cmd.
cmd /c "python -c ""import tkinterdnd2"" >nul 2>nul"
if ($LASTEXITCODE -eq 0) {
    $piArgs += @('--collect-all', 'tkinterdnd2')
    Note 'bundling tkinterdnd2 (drag-and-drop) with --collect-all'
} else {
    Note 'tkinterdnd2 not importable here; the build will fall back to Browse-only'
}
$piArgs += (Join-Path $Root 'app.py')

& pyinstaller @piArgs
if ($LASTEXITCODE -ne 0) { Die "PyInstaller failed with exit code $LASTEXITCODE" }
$ExePath = Join-Path $Dist "$AppSlug.exe"
if (-not (Test-Path $ExePath)) { Die "expected $ExePath but it was not produced" }
Note "built $ExePath"

# ------------------------------------------------------------------ 2) bundle
Say '2/5  Assembling the portable bundle'
if (Test-Path $Bundle) { Remove-Item -Recurse -Force $Bundle }
New-Item -ItemType Directory -Force -Path $Bundle, $PyDir, $JreDir | Out-Null

Copy-Item $ExePath (Join-Path $Bundle "$AppSlug.exe") -Force
Copy-Item (Join-Path $Root 'update_helper.ps1') $Bundle -Force
if (Test-Path (Join-Path $Root 'LICENSE.txt')) { Copy-Item (Join-Path $Root 'LICENSE.txt') $Bundle -Force }

# --- embedded CPython -------------------------------------------------------
$pyZip = Join-Path $Work 'python-embed.zip'
if (-not (Test-Path $pyZip)) {
    Note "downloading embedded Python: $PythonEmbedUrl"
    Invoke-WebRequest -Uri $PythonEmbedUrl -OutFile $pyZip -UseBasicParsing
}
Expand-Archive -Path $pyZip -DestinationPath $PyDir -Force

# CRITICAL: the embedded distribution ships with "import site" commented out in
# python3xx._pth. Without site, pip installs land in the folder but are NEVER
# importable, so the CLI silently will not exist. Uncomment it.
$pth = Get-ChildItem -Path $PyDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { Die "no python*._pth found in $PyDir - is this the embeddable zip?" }
$pthText = Get-Content $pth.FullName
$patched = $pthText -replace '^\s*#\s*import\s+site\s*$', 'import site'
if ($patched -notcontains 'import site') { $patched += 'import site' }
if ($patched -notcontains 'Lib\site-packages') { $patched += 'Lib\site-packages' }
Set-Content -Path $pth.FullName -Value $patched -Encoding ascii
Note "patched $($pth.Name): import site enabled"

# --- pip bootstrap ----------------------------------------------------------
$getPip = Join-Path $Work 'get-pip.py'
if (-not (Test-Path $getPip)) {
    Note 'downloading get-pip.py'
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
}
$pyExe = Join-Path $PyDir 'python.exe'
& $pyExe $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { Die "get-pip.py failed ($LASTEXITCODE)" }

$spec = if ($NoHybrid) { 'opendataloader-pdf' } else { 'opendataloader-pdf[hybrid]' }
Say "     pip install `"$spec`"  (this is the slow part)"
if (-not $NoHybrid) {
    Note 'the [hybrid] extra pulls torch/docling/easyocr - expect ~1 GB and several minutes'
}
& $pyExe -m pip install --no-warn-script-location --no-input $spec
if ($LASTEXITCODE -ne 0) { Die "pip install $spec failed ($LASTEXITCODE)" }

# Smoke-test the module invocation the app actually uses. pip's console-script
# .exe wrappers bake in an absolute interpreter path and break the moment the
# bundle is moved, so they are never a valid check that the bundle works.
& $pyExe -m opendataloader_pdf --help > $null
if ($LASTEXITCODE -ne 0) { Die "the bundled engine cannot run (python -m opendataloader_pdf failed)" }
& $pyExe -m opendataloader_pdf.hybrid_server --help > $null
if ($LASTEXITCODE -ne 0) { Note 'WARNING: bundled hybrid server did not respond to --help' }
Note 'bundled engine verified via python -m'

$engineVersion = (& $pyExe -m pip show opendataloader-pdf |
                  Select-String -Pattern '^Version:\s*(.+)$').Matches.Groups[1].Value.Trim()
if (-not $engineVersion) { Die 'could not read the installed engine version via pip show' }
Note "engine version: $engineVersion"

# --- portable JRE -----------------------------------------------------------
$jreZip = Join-Path $Work 'jre17.zip'
if (-not (Test-Path $jreZip)) {
    Note 'downloading Temurin JRE 17'
    Invoke-WebRequest -Uri $JreUrl -OutFile $jreZip -UseBasicParsing
}
$jreTmp = Join-Path $Work 'jre-extract'
if (Test-Path $jreTmp) { Remove-Item -Recurse -Force $jreTmp }
Expand-Archive -Path $jreZip -DestinationPath $jreTmp -Force
# The zip wraps everything in a jdk-17.x.y-jre folder; flatten it into runtime\jre.
$inner = Get-ChildItem -Path $jreTmp -Directory | Select-Object -First 1
if ($null -eq $inner) { Die "unexpected JRE archive layout in $jreTmp" }
Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $JreDir -Recurse -Force

$javaExe = Join-Path $JreDir 'bin\java.exe'
if (-not (Test-Path $javaExe)) { Die "bundled JRE is missing bin\java.exe" }
# java -version writes to STDERR; see the NativeCommandError note above.
$javaLines = cmd /c "`"$javaExe`" -version 2>&1"
$javaFirst = ($javaLines | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1)
if (-not $javaFirst) { Die "the bundled java.exe produced no version output" }
Note ("bundled java: " + $javaFirst.Trim())
if ($javaFirst -notmatch '"(\d+)') { Die "could not parse the bundled Java version: $javaFirst" }
elseif ([int]$Matches[1] -lt 11) { Die "bundled Java is $($Matches[1]); the engine needs 11+" }

# --- version.json -----------------------------------------------------------
$versionJson = [ordered]@{
    app    = $Version
    engine = $engineVersion
    jre    = 17
    built  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    repo   = $Repo
}
# BOM-less UTF-8. Set-Content -Encoding utf8 prepends a BOM in PS 5.1, which
# would make this file differ byte-wise from the copy update_helper.ps1 rewrites
# during a rollback - a spurious "changed file" in the next update's SHA diff.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $Bundle 'version.json'),
    ($versionJson | ConvertTo-Json -Depth 5), $utf8NoBom)
Note "wrote version.json (app=$Version engine=$engineVersion jre=17)"

$bundleSize = (Get-ChildItem $Bundle -Recurse -File | Measure-Object -Sum Length).Sum
Note ("bundle size: {0:N0} MB" -f ($bundleSize / 1MB))

# --------------------------------------------------------------------- 3) zip
Say '3/5  Creating the portable zip'
$zipName = "$AppSlug-$Version-portable.zip"
$zipPath = Join-Path $Dist $zipName
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
# Compress-Archive buffers the whole tree in memory - measured at 1.5 GB resident
# for this bundle. ZipFile::CreateFromDirectory streams to disk instead. It also
# writes the directory's *contents* at the archive root, matching the old layout.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Bundle, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)
$sha = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
Note "$zipName"
Note ("zip size: {0:N0} MB" -f ((Get-Item $zipPath).Length / 1MB))
Write-Host ""
Write-Host "    sha256: $sha" -ForegroundColor Yellow
Write-Host "    ^ paste this line into the GitHub release notes - the in-app" -ForegroundColor DarkGray
Write-Host "      updater refuses to install a download it cannot verify." -ForegroundColor DarkGray
Write-Host ""
Set-Content -Path (Join-Path $Dist "$zipName.sha256") -Value "sha256: $sha" -Encoding ascii

# ------------------------------------------------------------- 4) Inno Setup
Say '4/5  Inno Setup'
$iscc = $null
foreach ($cand in @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
    if ($cand -and (Test-Path $cand)) { $iscc = $cand; break }
}
if (-not $iscc) {
    Write-Host "    SKIPPED: Inno Setup (ISCC.exe) not found." -ForegroundColor Yellow
    Note 'Install it from https://jrsoftware.org/isdl.php, then re-run to get the setup exe.'
    Note "The portable zip above is complete and usable on its own."
} else {
    if (-not (Test-Path (Join-Path $Root 'LICENSE.txt'))) {
        Set-Content -Path (Join-Path $Root 'LICENSE.txt') -Encoding utf8 -Value @'
PDF to Data - personal use.

This installer bundles third-party components under their own licenses:
  * opendataloader-pdf (Apache-2.0)
  * Eclipse Temurin JRE 17 (GPLv2 with Classpath Exception)
  * CPython (PSF License)
'@
        Note 'created a default LICENSE.txt (edit it before shipping)'
    }
    & $iscc "/DAppVersion=$Version" "/DBundleDir=$Bundle" (Join-Path $Root 'PDFtoData.iss')
    if ($LASTEXITCODE -ne 0) { Die "ISCC failed ($LASTEXITCODE)" }
    Note "built dist\$AppSlug-Setup-$Version.exe"
}

# ------------------------------------------------------------ 5) GitHub release
Say '5/5  GitHub release'
if ($SkipRelease) {
    Note 'skipped (-SkipRelease)'
} else {
  $gh = $null
  foreach ($cand in @(
      (Get-Command gh -ErrorAction SilentlyContinue).Source,
      "$env:ProgramFiles\GitHub CLI\gh.exe",
      "${env:ProgramFiles(x86)}\GitHub CLI\gh.exe",
      "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe",
      "$env:LOCALAPPDATA\Microsoft\WinGet\Links\gh.exe")) {
      if ($cand -and (Test-Path $cand)) { $gh = $cand; break }
  }
  if (-not $gh) {
    Write-Host "    SKIPPED: the GitHub CLI (gh) was not found." -ForegroundColor Yellow
    Note 'Install from https://cli.github.com/ and run: gh auth login'
  } else {
    # gh writes its "not logged in" notice to stderr; use cmd so PowerShell does
    # not turn that into a NativeCommandError under $ErrorActionPreference=Stop.
    cmd /c "`"$gh`" auth status >nul 2>nul"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    SKIPPED: gh is installed but not logged in." -ForegroundColor Yellow
        Note 'Run: gh auth login   (then re-run this script to publish)'
        Note "Assets are ready to attach by hand: $zipPath"
        Say 'Done.'
        exit 0
    }
    $assets = @($zipPath)
    $setupExe = Join-Path $Dist "$AppSlug-Setup-$Version.exe"
    if (Test-Path $setupExe) { $assets += $setupExe }

    $notesArgs = @()
    if ($ChangelogFile -and (Test-Path $ChangelogFile)) {
        # Make sure the hash the updater verifies against is actually in the notes.
        $notesBody = Get-Content $ChangelogFile -Raw
        if ($notesBody -notmatch 'sha256:\s*[0-9a-fA-F]{64}') {
            $tmpNotes = Join-Path $Work "notes-$Version.md"
            [System.IO.File]::WriteAllText($tmpNotes, ($notesBody + "`n`nsha256: $sha`n"),
                (New-Object System.Text.UTF8Encoding($false)))
            $notesArgs = @('--notes-file', $tmpNotes)
            Note 'appended the sha256 line to the release notes'
        } else {
            $notesArgs = @('--notes-file', $ChangelogFile)
        }
    } else {
        $notesArgs = @('--notes', "Release $Version`n`nsha256: $sha")
        Note 'no -ChangelogFile given; generated minimal notes'
    }

    & $gh release create "v$Version" @assets --title "$AppSlug $Version" @notesArgs --draft
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    gh release create failed ($LASTEXITCODE)" -ForegroundColor Yellow
    } else {
        Note "draft release v$Version created - review and publish it on GitHub"
    }
  }
}

Say 'Done.'
Write-Host "  Portable zip : $zipPath"
if (Test-Path (Join-Path $Dist "$AppSlug-Setup-$Version.exe")) {
    Write-Host "  Installer    : $(Join-Path $Dist "$AppSlug-Setup-$Version.exe")"
}
Write-Host "  sha256       : $sha"
