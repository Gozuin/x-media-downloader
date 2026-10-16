# -*- coding: utf-8 -*-
"""只读排查：找出最近一次运行用到的配置、Cookie 项名（不打印值）、输出目录和下载记录。"""
import json
import os
import time

B = r'D:\AI\x-media-downloader'
N = 'X媒体下载器'
HOME = os.path.expanduser('~')
CFGS = [
    os.path.join(B, 'dist', N, 'config.json'),
    os.path.join(B, 'dist', 'config.json'),
    os.path.join(B, 'config.json'),
    os.path.join(os.environ.get('APPDATA', ''), 'XMediaDownloader', 'config.json'),
]
COOKIES = [p.replace('config.json', 'cookies.json') for p in CFGS]
KEYS = ('output_dir', 'proxy', 'use_proxy', 'auto_proxy', 'workers', 'browser_no_sandbox',
        'max_scrolls', 'max_tweets', 'queries', 'date_from', 'date_to', 'tab',
        'want_video', 'want_photo', 'want_gif', 'skip_existing', 'tz_mode')


def mtime(p):
    return time.strftime('%m-%d %H:%M:%S', time.localtime(os.path.getmtime(p)))


outs = set()
for p in CFGS:
    if not os.path.exists(p):
        print('CFG 不存在', p)
        continue
    try:
        c = json.load(open(p, encoding='utf-8'))
    except Exception as e:
        print('CFG 读失败', p, e)
        continue
    print('CFG', mtime(p), p)
    print('   ', json.dumps({k: c.get(k) for k in KEYS}, ensure_ascii=False))
    if c.get('output_dir'):
        outs.add(c['output_dir'])

for p in COOKIES:
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding='utf-8'))
            print('COOKIE', mtime(p), p, '项名:', sorted((d.get('cookies') or {}).keys()), 'handle:', d.get('handle'))
        except Exception as e:
            print('COOKIE 读失败', p, e)

outs.add(os.path.join(HOME, 'Downloads', 'X媒体下载'))
for o in outs:
    print('OUT', o, '存在' if os.path.isdir(o) else '不存在')
    if not os.path.isdir(o):
        continue
    n = 0
    for d, _ds, fs in os.walk(o):
        for f in fs:
            fp = os.path.join(d, f)
            n += 1
            if n <= 60:
                print('   ', mtime(fp), os.path.getsize(fp), fp)
    print('    共', n, '个文件')
    csvp = os.path.join(o, '_meta', 'downloads.csv')
    if os.path.exists(csvp):
        lines = open(csvp, encoding='utf-8-sig', errors='replace').read().splitlines()
        print('    downloads.csv 最后 10 行:')
        for line in lines[-10:]:
            print('     ', line)

for name in ('selftest-report.txt',):
    for base in (os.path.join(B, 'dist', N), os.path.join(B, 'dist'), B):
        p = os.path.join(base, name)
        if os.path.exists(p):
            print('REPORT', mtime(p), p)
