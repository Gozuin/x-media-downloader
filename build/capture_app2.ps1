Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Cap4 {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int ht, bool repaint);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr child, string cls, string title);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr param);
  public delegate bool EnumProc(IntPtr h, IntPtr param);
  public static IntPtr FindForPid(int pid, string cls) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      int wpid; GetWindowThreadProcessId(h, out wpid);
      if (wpid == pid && IsWindowVisible(h)) {
        var sb = new System.Text.StringBuilder(256);
        GetClassName(h, sb, 256);
        if (cls == null || sb.ToString().Contains(cls)) { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder sb, int max);
}
"@
$exe = $args[0]
$out = $args[1]
if (Test-Path $out) { Remove-Item $out -Force }
$p = Start-Process -FilePath $exe -PassThru
Write-Output ("started pid=" + $p.Id)
Start-Sleep -Seconds 6

# 找 Tk 主窗口（类名含 TkTopLevel），找不到再退回 MainWindowHandle
$h = [Cap4]::FindForPid($p.Id, "Tk")
Write-Output ("tk handle=" + $h)
$p.Refresh()
if ($h -eq [IntPtr]::Zero -and $p.MainWindowHandle -ne [IntPtr]::Zero) { $h = $p.MainWindowHandle }
if ($h -eq [IntPtr]::Zero) { Write-Output "no window"; exit 3 }

[Cap4]::MoveWindow($h, 2, 2, 980, 770, $true) | Out-Null
[Cap4]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Seconds 2

$r = New-Object Cap4+RECT
[Cap4]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $ht = $r.B - $r.T
Write-Output ("rect=" + $w + "x" + $ht + " at " + $r.L + "," + $r.T)
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ("saved=" + $out + " bytes=" + (Get-Item $out).Length)
Write-Output ("PID=" + $p.Id)
