# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['screen_bot/bot.py'],
    pathex=['screen_bot'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'tree_sitter',
        'tree_sitter_c_sharp',
        'tree_sitter_java',
        'tree_sitter_python',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ScreenBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['screen_bot/icon.ico'],
)
