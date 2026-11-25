# Removes exactly what install.ps1 added: the fastgrab folder and its PATH entry.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Stop"
$dir = Join-Path $env:LOCALAPPDATA "Programs\fastgrab"

$key = Get-Item "HKCU:\Environment"
$path = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
$entries = $path -split ";" | Where-Object { $_ -ne "" }
if ($entries -contains $dir) {
    $new = ($entries | Where-Object { $_ -ne $dir }) -join ";"
    Set-ItemProperty -Path "HKCU:\Environment" -Name "Path" -Value $new -Type ExpandString
    Write-Host "Removed $dir from your user PATH"
}

foreach ($f in "fastgrab.py", "fastgrab.cmd", "path-backup.txt") {
    $p = Join-Path $dir $f
    if (Test-Path $p) { Remove-Item $p }
}
if ((Test-Path $dir) -and -not (Get-ChildItem $dir -Force)) { Remove-Item $dir }
Write-Host "fastgrab uninstalled."
