# -*- coding: utf-8 -*-
"""在同一进程里构建界面并截图（绕开 venv 启动器找不到窗口的问题）。"""
import os
import subprocess
import sys
import tkinter as tk

PROJ = r"D:\AI\x-media-downloader"
sys.path.insert(0, PROJ)
from xdl import gui  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJ, "_shots", "modern-gui.png")
EXPAND = len(sys.argv) > 2 and sys.argv[2] == "adv"

gui.enable_dpi_awareness()
root = tk.Tk()
app = gui.App(root)
root.geometry("1120x900+20+20")
if EXPAND:
    root.after(800, app._toggle_advanced)


def shoot() -> None:
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    ps = (
        "Add-Type -AssemblyName System.Drawing;"
        f"$b=New-Object System.Drawing.Bitmap({w},{h});"
        "$g=[System.Drawing.Graphics]::FromImage($b);"
        f"$g.CopyFromScreen({x},{y},0,0,(New-Object System.Drawing.Size({w},{h})));"
        f"$b.Save('{OUT}',[System.Drawing.Imaging.ImageFormat]::Png);$g.Dispose();$b.Dispose()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    print("rc", r.returncode, (r.stderr or "")[:500])
    print("saved", OUT, os.path.exists(OUT) and os.path.getsize(OUT), f"{w}x{h}")
    root.destroy()


root.after(2500, shoot)
root.mainloop()
