# -*- coding: utf-8 -*-
'''打包收尾：先跑打包版自检，再组装发布目录，最后生成 zip。不会带上任何本机配置或 Cookie。'''
import os
import re
import shutil
import subprocess
import sys
import zipfile

# 项目根目录 = 本文件（build/）的上一级；换机器、换克隆路径都不用改
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = 'X媒体下载器'
sys.path.insert(0, PROJ)
from xdl import __version__  # noqa: E402

# Windows 控制台默认是 GBK，自检报告末行含 emoji（✅），直接 print 会 UnicodeEncodeError。
# 自检「全部通过」时才会走到这一步，所以以前一直是坏的。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass

DIST_DIR = os.path.join(PROJ, 'dist', NAME)
EXE = os.path.join(DIST_DIR, NAME + '.exe')
STAGE_ROOT = os.path.join(PROJ, 'build', 'zip-stage')
STAGE = os.path.join(STAGE_ROOT, NAME)
ZIP = os.path.join(PROJ, 'dist', f'{NAME}-v{__version__}-win64.zip')
SKIP = {'config.json', 'cookies.json', 'selftest-report.txt', '.write_probe', '_browser_profile', 'xdl_debug_port.txt'}
DOCS = ('使用说明.txt', '技术文档.md')


def main() -> int:
    print('version', __version__)
    if not os.path.isfile(EXE):
        print('找不到打包产物:', EXE)
        return 2

    # 1) 打包版自检（结果写在 exe 同级的 selftest-report.txt）
    report = os.path.join(DIST_DIR, 'selftest-report.txt')
    if os.path.exists(report):
        os.remove(report)
    proc = subprocess.run([EXE, '--selftest'], cwd=DIST_DIR, timeout=300)
    print('selftest exit', proc.returncode)
    text = ''
    if os.path.exists(report):
        with open(report, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        print('\n'.join(text.splitlines()[-4:]))
    # 只看自检汇总那一行。不能用 '\u5931\u8d25 0' not in text：
    # 报告正文里也有“下载完成：成功 0、跳过 4、失败 0”，子串匹配会恒真。
    summary = re.search(r'共\s*(\d+)\s*项检查[，,]\s*通过\s*(\d+)[，,]\s*失败\s*(\d+)', text)
    if not summary:
        print('自检报告里找不到汇总行，停止生成 zip')
        return 3
    if int(summary.group(3)) != 0:
        print('打包版自检没有全部通过'
              f'（通过 {summary.group(2)}/{summary.group(1)}，失败 {summary.group(3)}），停止生成 zip')
        return 3
    print(f'自检通过 {summary.group(2)}/{summary.group(1)}')

    # 2) 保留旧发布目录里的说明文档，并把文档里的版本号统一成当前版本
    docs = {}
    for name in DOCS:
        path = os.path.join(STAGE, name)
        if os.path.exists(path):
            with open(path, encoding='utf-8', errors='replace') as fh:
                docs[name] = re.sub(r'v\d+\.\d+\.\d+', f'v{__version__}', fh.read())
    print('docs kept:', list(docs))

    # 3) 重建发布目录：拷贝打包产物，排除本机配置、Cookie、自检报告和浏览器配置目录
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    shutil.copytree(DIST_DIR, STAGE, ignore=lambda _d, names: [n for n in names if n in SKIP])
    for name, content in docs.items():
        with open(os.path.join(STAGE, name), 'w', encoding='utf-8') as fh:
            fh.write(content)

    # 4) 生成 zip（顶层是「X媒体下载器」文件夹）
    if os.path.exists(ZIP):
        os.remove(ZIP)
    count = 0
    with zipfile.ZipFile(ZIP, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, _dirs, files in os.walk(STAGE):
            for fname in files:
                full = os.path.join(root, fname)
                zf.write(full, os.path.relpath(full, STAGE_ROOT))
                count += 1
    print('zip', ZIP, os.path.getsize(ZIP), 'bytes,', count, 'files')
    leaked = [n for n in zipfile.ZipFile(ZIP).namelist() if os.path.basename(n) in SKIP]
    print('leaked private files:', leaked or 'none')
    return 0


if __name__ == '__main__':
    sys.exit(main())
