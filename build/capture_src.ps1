param([string]$py, [string]$out)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class C8 {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int ht,bool p);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool EnumWindows(Dlg cb, IntPtr p);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder sb, int m);
  public delegate bool Dlg(IntPtr h, IntPtr p);
  public static IntPtr Find(int pid, string cls) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      int wp; GetWindowThreadProcessId(h, out wp);
      if (wp == pid && IsWindowVisible(h)) {
        var sb = new System.Text.StringBuilder(256); GetClassName(h, sb, 256);
        if (cls == null || sb.ToString().Contains(cls)) { found = h; return false; }
      } return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
$proj = "D:\AI\x-media-downloader"
if (Test-Path $out) { Remove-Item $out -Force }
$p = Start-Process -FilePath $py -ArgumentList '-X','utf8','main.py' -WorkingDirectory $proj -PassThru
$h = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 500
  if ($p.HasExited) { Write-Output ("process exited early: " + $p.ExitCode); exit 2 }
  # venv 的 python.exe 在 Windows 上只是启动器，真正的窗口属于它的子进程
  $ids = @($p.Id) + @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $p.Id) | ForEach-Object { [int]$_.ProcessId })
  foreach ($id in $ids) { $h = [C8]::Find([int]$id, "Tk"); if ($h -ne [IntPtr]::Zero) { break } }
  if ($h -ne [IntPtr]::Zero) { break }
}
if ($h -eq [IntPtr]::Zero) { Write-Output "no window"; $ids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; exit 3 }
[void][C8]::ShowWindow($h, 9)
[void][C8]::MoveWindow($h, 0, 0, 1120, 900, $true)
[void][C8]::SetForegroundWindow($h)
Start-Sleep -Seconds 3
$r = New-Object C8+RECT
[void][C8]::GetWindowRect($h, [ref]$r)
$w = $r.R - $r.L; $ht = $r.B - $r.T
Write-Output ("rect=" + $w + "x" + $ht)
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ("saved=" + $out + " bytes=" + (Get-Item $out).Length)
$ids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
