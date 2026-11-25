# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import ssl

# Some build runtimes load a companion certificate policy from SSLContext.__new__.
# Keep that policy alongside the frozen ssl module; do not disable verification.
tls_policy = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
tls_data = [(str(tls_policy), '.')] if tls_policy.is_file() else []


a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('index.html', '.'),
        ('app.js', '.'),
        ('styles.css', '.'),
        ('privacy.html', '.'),
        ('help.html', '.'),
        ('guide.html', '.'),
        ('邮箱绑定指南.md', '.'),
        ('pumail-logo.svg', '.'),
        ('carton1.svg', '.'),
        ('carton4.svg', '.'),
        ('gaia-tree.png', '.'),
        ('notice14.mp3', '.'),
        ('guide-images', 'guide-images'),
    ] + tls_data,
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='PuMail',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='electron/build/icon.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PuMail',
)
