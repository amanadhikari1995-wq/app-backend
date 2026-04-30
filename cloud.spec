# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the cloud relay connector (sdk/wd_cloud.py).
# Run:  pyinstaller cloud.spec --clean
#
# Produces a single-file watchdog-cloud.exe in dist/.

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs


hiddenimports  = []
hiddenimports += collect_submodules('websockets')
hiddenimports += collect_submodules('cryptography')
hiddenimports += [
    'httpx', 'h11', 'h2', 'idna', 'sniffio',
    'dotenv',
]

binaries  = []
binaries += collect_dynamic_libs('cryptography')

block_cipher = None

a = Analysis(
    ['sdk/wd_cloud.py'],
    pathex=['.'],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'pandas',
        'PIL', 'IPython', 'pytest', 'unittest',
    ],
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
    name='watchdog-cloud',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../frontend/build/icon.ico' if __import__('os').path.exists('../frontend/build/icon.ico') else None,
)
