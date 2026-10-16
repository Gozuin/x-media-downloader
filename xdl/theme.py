# -*- coding: utf-8 -*-
'''现代化浅色主题：配色、字体、ttk 样式，以及卡片 / 状态徽章等小组件（纯 tkinter，不加新依赖）。'''
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

C = {
    'bg': '#F3F5FA',
    'card': '#FFFFFF',
    'border': '#E2E7F0',
    'text': '#1F2937',
    'muted': '#6B7280',
    'primary': '#3B6EF5',
    'primary_hover': '#2F5BD6',
    'primary_press': '#244AB5',
    'primary_soft': '#E8EEFF',
    'ok': '#15803D', 'ok_soft': '#DCFCE7',
    'warn': '#B45309', 'warn_soft': '#FEF3C7',
    'err': '#B91C1C', 'err_soft': '#FEE2E2',
    'idle': '#475569', 'idle_soft': '#EEF2F7',
    'log_bg': '#0F172A', 'log_fg': '#E2E8F0',
}

FONT = 'Microsoft YaHei UI'
MONO = 'Consolas'


def apply_theme(root: tk.Misc) -> ttk.Style:
    global MONO
    families = set(tkfont.families(root))
    for name in ('Cascadia Mono', 'JetBrains Mono', 'Consolas'):
        if name in families:
            MONO = name
            break
    root.configure(bg=C['bg'])
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=C['bg'], foreground=C['text'], font=(FONT, 10),
                    bordercolor=C['border'], lightcolor=C['border'], darkcolor=C['border'],
                    focuscolor=C['primary'], troughcolor=C['idle_soft'])
    style.configure('TFrame', background=C['bg'])
    style.configure('Card.TFrame', background=C['card'])
    style.configure('TLabel', background=C['bg'])
    style.configure('Card.TLabel', background=C['card'])
    style.configure('Muted.TLabel', background=C['card'], foreground=C['muted'], font=(FONT, 9))
    style.configure('Title.TLabel', background=C['bg'], font=(FONT, 16, 'bold'))
    style.configure('Sub.TLabel', background=C['bg'], foreground=C['muted'], font=(FONT, 9))
    style.configure('CardTitle.TLabel', background=C['card'], font=(FONT, 11, 'bold'))
    style.configure('Path.TLabel', background=C['card'], foreground=C['primary'], font=(FONT, 9))

    style.configure('TButton', background=C['card'], foreground=C['text'], padding=(12, 6),
                    borderwidth=1, relief='flat')
    style.map('TButton', background=[('disabled', C['idle_soft']), ('pressed', C['border']), ('active', C['idle_soft'])],
              foreground=[('disabled', '#A0A8B5')])
    style.configure('Accent.TButton', background=C['primary'], foreground='#FFFFFF',
                    bordercolor=C['primary'], lightcolor=C['primary'], darkcolor=C['primary'],
                    padding=(18, 8), font=(FONT, 10, 'bold'))
    style.map('Accent.TButton',
              background=[('disabled', '#A9BCF5'), ('pressed', C['primary_press']), ('active', C['primary_hover'])],
              foreground=[('disabled', '#F1F4FF')])
    style.configure('Soft.TButton', background=C['primary_soft'], foreground=C['primary'],
                    bordercolor=C['primary_soft'], lightcolor=C['primary_soft'], darkcolor=C['primary_soft'],
                    padding=(14, 7), font=(FONT, 10, 'bold'))
    style.map('Soft.TButton', background=[('disabled', C['idle_soft']), ('pressed', '#D5E0FF'), ('active', '#DCE5FF')],
              foreground=[('disabled', '#A0A8B5')])
    style.configure('Danger.TButton', background=C['card'], foreground=C['err'], padding=(14, 7))
    style.map('Danger.TButton', background=[('disabled', C['idle_soft']), ('active', C['err_soft'])],
              foreground=[('disabled', '#A0A8B5')])
    style.configure('Link.TButton', background=C['card'], foreground=C['primary'], borderwidth=0,
                    bordercolor=C['card'], lightcolor=C['card'], darkcolor=C['card'], padding=(4, 2))
    style.map('Link.TButton', background=[('active', C['primary_soft'])])
    style.configure('Link.TButton', width=0)
    style.configure('TButton', width=0)

    for name in ('TEntry', 'TSpinbox', 'TCombobox'):
        style.configure(name, fieldbackground='#FFFFFF', bordercolor=C['border'],
                        lightcolor=C['border'], darkcolor=C['border'], padding=(6, 4), arrowsize=13)
        style.map(name, bordercolor=[('focus', C['primary'])], lightcolor=[('focus', C['primary'])])
    style.configure('Bad.TEntry', fieldbackground=C['err_soft'], bordercolor=C['err'],
                    lightcolor=C['err'], darkcolor=C['err'])

    for name in ('TCheckbutton', 'TRadiobutton'):
        style.configure(name, background=C['card'], foreground=C['text'], indicatorcolor='#FFFFFF',
                        indicatorbackground='#FFFFFF', padding=(2, 3))
        style.map(name, background=[('active', C['card'])],
                  indicatorcolor=[('selected', C['primary']), ('pressed', C['primary_soft'])],
                  indicatorbackground=[('selected', C['primary']), ('pressed', C['primary_soft'])],
                  indicatorforeground=[('selected', '#FFFFFF')])
        style.configure(name, indicatorforeground='#FFFFFF', indicatorsize=14)
    # 芯片式开关 / 分段选择：选中时整块变成浅蓝
    style.configure('Chip.TCheckbutton', background=C['idle_soft'], padding=(10, 5))
    style.map('Chip.TCheckbutton', background=[('selected', C['primary_soft']), ('active', '#E3E9F7')],
              foreground=[('selected', C['primary'])])
    style.configure('Seg.TRadiobutton', background=C['idle_soft'], padding=(12, 5))
    style.map('Seg.TRadiobutton', background=[('selected', C['primary_soft']), ('active', '#E3E9F7')],
              foreground=[('selected', C['primary'])])
    # 芯片 / 分段按钮去掉原生的小圆点和小方框，只靠底色区分选中状态
    style.layout('Chip.TCheckbutton', [('Checkbutton.padding', {'sticky': 'nswe', 'children': [
        ('Checkbutton.label', {'sticky': 'nswe'})]})])
    style.layout('Seg.TRadiobutton', [('Radiobutton.padding', {'sticky': 'nswe', 'children': [
        ('Radiobutton.label', {'sticky': 'nswe'})]})])
    style.configure('Chip.TCheckbutton', font=(FONT, 10))
    style.map('Chip.TCheckbutton', font=[('selected', (FONT, 10, 'bold'))])
    style.configure('Seg.TRadiobutton', font=(FONT, 10))
    style.map('Seg.TRadiobutton', font=[('selected', (FONT, 10, 'bold'))])

    style.configure('Horizontal.TProgressbar', troughcolor=C['idle_soft'], background=C['primary'],
                    bordercolor=C['idle_soft'], lightcolor=C['primary'], darkcolor=C['primary'], thickness=8)
    style.configure('Vertical.TScrollbar', background=C['bg'], troughcolor=C['bg'], bordercolor=C['bg'],
                    arrowcolor=C['muted'], gripcount=0)
    style.map('Vertical.TScrollbar', background=[('active', C['border'])])
    return style


class Card(tk.Frame):
    '''白色卡片：细边框 + 标题行 + 内容区（self.body）。'''

    def __init__(self, master, title: str = '', subtitle: str = ''):
        super().__init__(master, bg=C['card'], highlightthickness=1,
                         highlightbackground=C['border'], highlightcolor=C['border'], bd=0)
        self.head = None
        if title:
            head = ttk.Frame(self, style='Card.TFrame')
            head.pack(fill='x', padx=16, pady=(14, 2))
            ttk.Label(head, text=title, style='CardTitle.TLabel').pack(side='left')
            if subtitle:
                ttk.Label(head, text=subtitle, style='Muted.TLabel').pack(side='left', padx=(10, 0))
            self.head = head
        self.body = ttk.Frame(self, style='Card.TFrame')
        self.body.pack(fill='both', expand=True, padx=16, pady=(8, 14))


class Badge(tk.Label):
    '''状态徽章：圆点 + 文字，按 ok / warn / err / idle / busy 切换配色。'''

    KINDS = {
        'ok': ('ok', 'ok_soft'), 'warn': ('warn', 'warn_soft'), 'err': ('err', 'err_soft'),
        'idle': ('idle', 'idle_soft'), 'busy': ('primary', 'primary_soft'),
    }

    def __init__(self, master, text: str = '', kind: str = 'idle'):
        super().__init__(master, font=(FONT, 9, 'bold'), padx=10, pady=4, bd=0)
        self.set(kind, text)

    def set(self, kind: str, text: str) -> None:
        fg, bg = self.KINDS.get(kind, self.KINDS['idle'])
        self.configure(text='● ' + text, fg=C[fg], bg=C[bg])


def style_text(widget: tk.Text, mono: bool = False, dark: bool = False) -> None:
    '''统一多行文本框外观；dark=True 用于日志区。'''
    if dark:
        widget.configure(bg=C['log_bg'], fg=C['log_fg'], insertbackground=C['log_fg'],
                         selectbackground='#334155', font=(MONO, 9), padx=12, pady=10,
                         relief='flat', bd=0, highlightthickness=0)
        widget.tag_configure('info', foreground=C['log_fg'])
        widget.tag_configure('ok', foreground='#4ADE80')
        widget.tag_configure('warn', foreground='#FBBF24')
        widget.tag_configure('err', foreground='#F87171')
        widget.tag_configure('dim', foreground='#64748B')
    else:
        widget.configure(bg='#FFFFFF', fg=C['text'], insertbackground=C['text'],
                         selectbackground=C['primary_soft'], font=(MONO if mono else FONT, 10),
                         padx=10, pady=8, relief='flat', bd=0, highlightthickness=1,
                         highlightbackground=C['border'], highlightcolor=C['primary'])


_OK_WORDS = ('成功', '通过', '正常', '已登录', '完成', '可用')
_WARN_WORDS = ('提示', '警告', '跳过', '停止', '未确认', '未登录', '兼容模式', '重试')
_ERR_WORDS = ('失败', '出错', '错误', '异常', '不可用', 'Error', 'Exception')


def log_tag(line: str) -> str:
    '''按关键词给日志行分级上色。'''
    if any(w in line for w in _ERR_WORDS) and '失败 0' not in line:
        return 'err'
    if any(w in line for w in _WARN_WORDS) and '跳过 0' not in line:
        return 'warn'
    if any(w in line for w in _OK_WORDS):
        return 'ok'
    if line.startswith('=') or line.startswith('—'):
        return 'dim'
    return 'info'
