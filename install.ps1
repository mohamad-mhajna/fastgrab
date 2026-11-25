# fastgrab installer for Windows (current user only, no admin needed)
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# What it does, and nothing else:
#   1. copies fastgrab.py into %LOCALAPPDATA%\Programs\fastgrab
#   2. creates fastgrab.cmd next to it, so you can type "fastgrab URL"
#   3. appends that folder to YOUR user PATH (other PATH entries are untouched;
#      the original value is saved to path-backup.txt first)
# Undo everything with uninstall.ps1.

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "fastgrab.py"
$dir = Join-Path $env:LOCALAPPDATA "Programs\fastgrab"

if (-not (Test-Path $src)) { throw "fastgrab.py not found next to install.ps1" }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "The Python launcher 'py' was not found. Install Python from python.org first."
}

New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item $src (Join-Path $dir "fastgrab.py") -Force
Set-Content -Path (Join-Path $dir "fastgrab.cmd") -Encoding ASCII -Value '@py "%~dp0fastgrab.py" %*'
Write-Host "Copied fastgrab to $dir"

# Read the user PATH raw (keeps %VARIABLES% unexpanded), append only if missing.
$key = Get-Item "HKCU:\Environment"
$path = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
$entries = $path -split ";" | Where-Object { $_ -ne "" }
if ($entries -contains $dir) {
    Write-Host "PATH already contains $dir"
} else {
    $backup = Join-Path $dir "path-backup.txt"
    if (-not (Test-Path $backup)) { Set-Content -Path $backup -Value $path -Encoding UTF8 }
    $new = if ($path -and -not $path.EndsWith(";")) { "$path;$dir" } elseif ($path) { "$path$dir" } else { $dir }
    Set-ItemProperty -Path "HKCU:\Environment" -Name "Path" -Value $new -Type ExpandString
    Write-Host "Added $dir to your user PATH (backup: $backup)"

    # Tell Windows the environment changed, so new terminals see it.
    Add-Type -Namespace Win32 -Name Env -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg,
    System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);
'@
    $r = [UIntPtr]::Zero
    [Win32.Env]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$r) | Out-Null
}

Write-Host ""
Write-Host "Installed. Open a NEW terminal and run:  fastgrab URL"
