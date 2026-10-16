Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Cap {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
}
"@

$proj = "D:\AI\x-media-downloader"
$shots = Join-Path $proj "_shots"
New-Item -ItemType Directory -Force -Path $shots | Out-Null
$out = Join-Path $shots "ui.png"
if (Test-Path $out) { Remove-Item $out -Force }

$p = Start-Process -FilePath "python" -ArgumentList "main.py" -WorkingDirectory $proj -PassThru
Write-Output "started pid=$($p.Id)"

$h = [IntPtr]::Zero
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    $p.Refresh()
    if ($p.HasExited) { Write-Output "process exited early: $($p.ExitCode)"; break }
    if ($p.MainWindowHandle -ne [IntPtr]::Zero -and [Win32Cap]::IsWindowVisible($p.MainWindowHandle)) { $h = $p.MainWindowHandle; break }
}
if ($h -eq [IntPtr]::Zero) { Write-Output "no window handle"; exit 2 }

Write-Output "window handle=$h  title=$($p.MainWindowTitle)"
[Win32Cap]::ShowWindow($h, 5) | Out-Null
[Win32Cap]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 1200

$r = New-Object Win32Cap+RECT
[Win32Cap]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $ht = $r.B - $r.T
Write-Output "rect=$($r.L),$($r.T),$w x $ht"

$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved: $out  size=$((Get-Item $out).Length) bytes"
Write-Output "PID_TO_KILL=$($p.Id)"
