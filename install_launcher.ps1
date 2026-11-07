# Install (or remove) PartsBin shortcuts on Windows.
#   .\install_launcher.ps1                 Start menu shortcut
#   .\install_launcher.ps1 -Desktop        also a Desktop shortcut
#   .\install_launcher.ps1 -Uninstall      remove both
# Easier: double-click install_launcher.cmd, which runs this with the right settings.
# The shortcut records this folder's path, so run it again if you move PartsBin.
param([switch]$Desktop, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$app = $PSScriptRoot
$links = @{
    Start = Join-Path ([Environment]::GetFolderPath('Programs')) 'PartsBin.lnk'
    Desk  = Join-Path ([Environment]::GetFolderPath('Desktop')) 'PartsBin.lnk'
}

if ($Uninstall) {
    Remove-Item $links.Start, $links.Desk -ErrorAction SilentlyContinue
    Write-Host 'Removed the PartsBin shortcuts.'
    exit 0
}

# windowed Python (no console box): a .venv next to the program, else pythonw, else the py launcher
$prefix = ''
$venv = Join-Path $app '.venv\Scripts\pythonw.exe'
if (Test-Path $venv) {
    $py = $venv
} elseif (Get-Command pythonw.exe -ErrorAction SilentlyContinue) {
    $py = (Get-Command pythonw.exe).Source
} elseif (Get-Command pyw.exe -ErrorAction SilentlyContinue) {
    $py = (Get-Command pyw.exe).Source
    $prefix = '-3 '
} else {
    Write-Error 'Python 3 was not found. Install it from python.org (tick "Add python.exe to PATH").'
}

$shell = New-Object -ComObject WScript.Shell
function New-Link($path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $py
    $s.Arguments = $prefix + '"' + (Join-Path $app 'partsbin.py') + '"'
    $s.WorkingDirectory = $app
    $s.IconLocation = (Join-Path $app 'icon.ico')
    $s.Description = 'Catalogue of electronic components in stock'
    $s.Save()
    Write-Host "Created $path"
}

New-Link $links.Start
if ($Desktop) { New-Link $links.Desk }
