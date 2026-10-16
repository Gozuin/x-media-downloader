# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir + 控制台子系统）。

所有路径都相对本文件解析，换机器 / 换克隆目录都不用改。
用法（项目根目录下）：

    python -m PyInstaller --noconfirm --clean --distpath dist --workpath build/work build\X媒体下载器.spec

注意：用 --windowed 打包出的 GUI 子系统进程在某些加固环境会被直接杀掉，
所以这里用 console=True（控制台子系统），程序启动后自己把控制台窗口藏起来。
"""
import os

SPEC_DIR = os.path.abspath(SPECPATH)
ROOT = os.path.abspath(os.path.join(SPEC_DIR, os.pardir))

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'numpy', 'pandas', 'PIL', 'lxml'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='X媒体下载器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(SPEC_DIR, 'version_info.txt'),
    icon=[os.path.join(SPEC_DIR, 'icon.ico')],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='X媒体下载器',
)
