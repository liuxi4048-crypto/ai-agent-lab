<#
  make-dist.ps1 - Build a clean, self-contained distribution of AI Agent Lab.

  Produces:
    dist\ai-agent-lab\      clean source tree (no personal / runtime data)
    dist\ai-agent-lab.zip   ready to hand out (extracts to ai-agent-lab\)

  What ships: every git-tracked file EXCEPT the author's personal data
  (projects\ test outputs and .claude\ editor config). Runtime dirs, logs,
  __pycache__ and node_modules are untracked, so they are never included.

  Usage:
    powershell -ExecutionPolicy Bypass -File make-dist.ps1
    powershell -ExecutionPolicy Bypass -File make-dist.ps1 -NoZip
#>
[CmdletBinding()]
param(
    [switch]$NoZip
)
$ErrorActionPreference = 'Stop'
$Root  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name  = 'ai-agent-lab'
$Dist  = Join-Path $Root 'dist'
$Stage = Join-Path $Dist $Name

# --- canonical file list from git (only tracked files ever ship) ----------
Push-Location $Root
try { $tracked = & git ls-files } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'git ls-files failed (not a git repository?)' }

# personal / machine-specific paths that must never be distributed
$excludePrefixes = @('projects/', '.claude/')
# include these even if not committed yet (LICENSE / this script)
$always = @('LICENSE', 'make-dist.ps1')

$set = New-Object System.Collections.Generic.HashSet[string]
foreach ($f in $tracked) {
    if ([string]::IsNullOrWhiteSpace($f)) { continue }
    $skip = $false
    foreach ($p in $excludePrefixes) { if ($f -like "$p*") { $skip = $true; break } }
    if (-not $skip) { [void]$set.Add($f) }
}
foreach ($f in $always) { if (Test-Path (Join-Path $Root $f)) { [void]$set.Add($f) } }

# --- rebuild the staging tree ---------------------------------------------
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

$count = 0
foreach ($rel in $set) {
    $src = Join-Path $Root $rel
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $Stage $rel
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $count++
}

# keep an empty projects\ so the app has a place to write on first run
$keepDir = Join-Path $Stage 'projects'
New-Item -ItemType Directory -Path $keepDir -Force | Out-Null
Set-Content -Path (Join-Path $keepDir '.gitkeep') -Value '' -Encoding ascii

Write-Host ("staged {0} files -> {1}" -f $count, $Stage)

# --- zip (top-level folder = ai-agent-lab\) -------------------------------
if (-not $NoZip) {
    $zip = Join-Path $Dist "$Name.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $Stage, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
    Write-Host ("zip: {0} ({1} MB)" -f $zip, $mb)
}
