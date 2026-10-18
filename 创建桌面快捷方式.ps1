$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$pythonwPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
$launcherPath = Join-Path $projectPath 'desktop_launcher.py'
if (!(Test-Path -LiteralPath $pythonwPath) -or !(Test-Path -LiteralPath $launcherPath)) {
    throw '找不到记账启动程序或 Python，请先检查 Python 是否安装完整。'
}

# Generate a local icon without fetching assets or accessing the ledger.
$iconFolder = Join-Path $projectPath 'assets'
New-Item -ItemType Directory -Path $iconFolder -Force | Out-Null
$iconPath = Join-Path $iconFolder 'ledger.ico'
if (!(Test-Path -LiteralPath $iconPath)) {
    Add-Type -AssemblyName System.Drawing
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LedgerIconHandle {
    [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr handle);
}
'@
    $bitmap = New-Object System.Drawing.Bitmap 64,64
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.Clear([System.Drawing.Color]::FromArgb(47,91,86))
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $font = New-Object System.Drawing.Font 'Arial',36,([System.Drawing.FontStyle]::Bold)
    $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(249,234,177))
    $format = New-Object System.Drawing.StringFormat
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $graphics.DrawString([string][char]0x00A5, $font, $brush, ([System.Drawing.RectangleF]::new(0,0,64,64)), $format)
    $iconHandle = $bitmap.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($iconHandle)
    $stream = [System.IO.File]::Create($iconPath)
    try { $icon.Save($stream) }
    finally {
        $stream.Dispose()
        $icon.Dispose()
        [LedgerIconHandle]::DestroyIcon($iconHandle) | Out-Null
        $format.Dispose()
        $brush.Dispose()
        $font.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath '记账.lnk'
$shell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    $existing = $shell.CreateShortcut($shortcutPath)
    if ($existing.TargetPath -ne $pythonwPath -or $existing.Arguments -notlike '*desktop_launcher.py*') {
        $backupPath = Join-Path $desktopPath ('记账-原快捷方式-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.lnk')
        Copy-Item -LiteralPath $shortcutPath -Destination $backupPath
    }
}
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwPath
$shortcut.Arguments = '-X utf8 "' + $launcherPath + '"'
$shortcut.WorkingDirectory = $projectPath
$shortcut.IconLocation = $iconPath + ',0'
$shortcut.Description = '启动本地双币账本并打开页面；服务已运行时直接进入。'
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output ('桌面快捷方式已创建：' + $shortcutPath)
