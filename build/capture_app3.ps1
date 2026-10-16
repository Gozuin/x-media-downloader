Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Drawing;
public class Cap6 {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int ht,bool p);
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
$exe = $args[0]
$out = $args[1]
if (Test-Path $out) { Remove-Item $out -Force }
$p = Start-Process -FilePath $exe -PassThru
Write-Output ("started pid=" + $p.Id)
Start-Sleep -Seconds 7
$h = [Cap6]::Find($p.Id, "Tk")
if ($h -eq [IntPtr]::Zero) { Write-Output "no tk window"; exit 3 }
[Cap6]::MoveWindow($h, 10, 10, 1000, 780, $true) | Out-Null
Start-Sleep -Seconds 2
$r = New-Object Cap6+RECT
[Cap6]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $ht = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
# flags=2 -> PW_RENDERFULLCONTENT（能抓到被遮挡的窗口内容）
$ok = [Cap6]::PrintWindow($h, $hdc, 2)
$g.ReleaseHdc($hdc)
$g.Dispose()
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output ("PrintWindow=" + $ok + " rect=" + $w + "x" + $ht + " saved=" + $out + " bytes=" + (Get-Item $out).Length)
Write-Output ("PID=" + $p.Id)
