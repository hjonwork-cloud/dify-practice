# -*- mode: python ; coding: utf-8 -*-
"""
DWHF ChatBot Watchdog GUI - PyInstaller spec
아이콘을 바꾸려면 icon= 경로만 수정 후 다시 빌드:
  e:\git-copilot\.conda\Scripts\pyinstaller.exe watchdog_gui.spec
"""

# ── 원하는 아이콘 선택 (4가지 중 하나) ──
# v1_kakao_yellow  : 검정 원 + 노랑 말풍선 + 파란 DW
# v2_dongwon_blue  : 파랑 사각형 + 흰 말풍선 + DW
# v3_bubble_dots   : 파랑 원 + 흰 말풍선 + 점3개 (기본값)
# v4_gradient      : 그라디언트 파랑 + 흰 말풍선 + DW
ICON = r"e:\git-copilot\dify-practice\icons\servercheck.ico"

block_cipher = None

a = Analysis(
    ['watchdog_gui.py'],
    pathex=[r'e:\git-copilot\dify-practice'],
    binaries=[],
    datas=[
        (r'e:\git-copilot\dify-practice\icons', 'icons'),
    ],
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.scrolledtext',
        '_tkinter',
        'urllib.request',
        'urllib.error',
        'json',
        'threading',
        'subprocess',
        'time',
        'os',
        'sys',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas', 'scipy', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DWHF_ChatBot_Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 콘솔 창 없음 (GUI only)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version_file=None,
)
