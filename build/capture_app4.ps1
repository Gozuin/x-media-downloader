Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class C7 {
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
      if (wp == pid) {
        var sb = new System.Text.StringBuilder(256); GetClassName(h, sb, 256);
        if (cls == null || sb.ToString().Contains(cls)) { found = h; return false; }
      } return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
$exe = $args[0]; $out = $args[1]
if (Test-Path $out) { Remove-Item $out -Force }
$p = Start-Process -FilePath $exe -PassThru
Start-Sleep -Seconds 7
$h = [C7]::Find($p.Id, "Tk")
if ($h -eq [IntPtr]::Zero) { Write-Output "no window"; exit 3 }
[void][C7]::ShowWindow($h, 9)          # SW_RESTORE
[void][C7]::ShowWindow($h, 5)          # SW_SHOW
[void][C7]::MoveWindow($h, 0, 0, 1000, 790, $true)
[void][C7]::SetForegroundWindow($h)
Start-Sleep -Seconds 3
$r = New-Object C7+RECT
[void][C7]::GetWindowRect($h, [ref]$r)
$w = $r.R - $r.L; $ht = $r.B - $r.T
Write-Output ("rect=" + $w + "x" + $ht + " at " + $r.L + "," + $r.T)
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ("saved=" + $out + " bytes=" + (Get-Item $out).Length)
Write-Output ("PID=" + $p.Id)
