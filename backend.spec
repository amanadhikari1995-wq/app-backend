# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the WATCH-DOG FastAPI backend.
# Run:  pyinstaller backend.spec --clean
#
# Produces a single-file watchdog-backend.exe in dist/.
#
# Why this is more than just `--onefile`: FastAPI + uvicorn + langchain +
# ccxt + apscheduler all use deferred / dynamic imports that PyInstaller's
# default static analysis misses. Each `hiddenimports` entry below is a
# real import that will silently fail at runtime if omitted.

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)


# ── Routers (imported by string in app.main) ─────────────────────────────
ROUTERS = [
    'app.routers.bots',
    'app.routers.api_connections',
    'app.routers.dashboard',
    'app.routers.trades',
    'app.routers.trainer',
    'app.routers.news',
    'app.routers.photos',
    'app.routers.notes',
    'app.routers.user_files',
    'app.routers.finance',
    'app.routers.whop',
    'app.routers.system_stats',
    'app.routers.ai_models',
    'app.routers.analyze',
    'app.routers.chat',
    'app.routers.sessions',
    'app.routers.auth',
]

# ── Session detectors (registered via @register_detector decorator on
#    import — PyInstaller's static analyser doesn't see they're needed). ──
SESSION_MODULES = [
    'app.session',
    'app.session.base',
    'app.session.types',
    'app.session.registry',
    'app.session.manager',
    'app.session.router',
    'app.session.detectors',
    'app.session.detectors.crypto_24x7',
    'app.session.detectors.kalshi_15m',
    'app.session.detectors.stocks_us',
]

# ── Other app modules that might be loaded dynamically ───────────────────
APP_MODULES = [
    'app.bot_manager',
    'app.token_tracker',
    'app.auth',
    'app.database',
    'app.models',
    'app.schemas',
]

# ── Submodules that PyInstaller's static analysis doesn't catch ──────────
hiddenimports  = []
hiddenimports += ROUTERS
hiddenimports += SESSION_MODULES
hiddenimports += APP_MODULES
# Catch any submodules under app.session.detectors that get added later
hiddenimports += collect_submodules('app.session.detectors')
hiddenimports += collect_submodules('uvicorn')           # protocols, lifespans
hiddenimports += collect_submodules('uvicorn.protocols')
hiddenimports += collect_submodules('uvicorn.loops')
hiddenimports += collect_submodules('uvicorn.lifespan')
hiddenimports += collect_submodules('starlette')
hiddenimports += collect_submodules('apscheduler')        # cron / interval triggers
hiddenimports += collect_submodules('passlib')            # bcrypt backend
hiddenimports += collect_submodules('passlib.handlers')
hiddenimports += collect_submodules('jose')               # python-jose (JWT)
hiddenimports += collect_submodules('email_validator')    # pydantic[email]
hiddenimports += collect_submodules('sqlalchemy.dialects')
hiddenimports += [
    'bcrypt',
    'pkg_resources.py2_warn',
    'pkg_resources.markers',
    'multipart',                # python-multipart
    'asyncio',
    'logging.config',
]

# ccxt is huge and imports exchanges lazily. Pull all submodules so any
# exchange the user picks at runtime is available.
hiddenimports += collect_submodules('ccxt')

# langchain has a sprawling plug-in module tree; collect what we use.
hiddenimports += collect_submodules('langchain')
hiddenimports += collect_submodules('langchain_core')
hiddenimports += collect_submodules('langchain_community')
hiddenimports += collect_submodules('langchain_anthropic')
hiddenimports += collect_submodules('langgraph')


# ── Data files (templates, JSON configs, etc. that ship with libs) ───────
datas  = []
datas += collect_data_files('uvicorn')
datas += collect_data_files('starlette')
datas += collect_data_files('langchain')
datas += collect_data_files('langchain_core')
datas += collect_data_files('langchain_community')
datas += collect_data_files('ccxt')
datas += collect_data_files('email_validator')


# ── Native libraries (e.g. bcrypt's _bcrypt.cpython.pyd) ─────────────────
binaries  = []
binaries += collect_dynamic_libs('bcrypt')
binaries += collect_dynamic_libs('cryptography')


# ── Build ────────────────────────────────────────────────────────────────
block_cipher = None

a = Analysis(
    ['run_backend.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Strip out things we definitely don't use to shrink the bundle
        'tkinter', 'matplotlib', 'numpy.tests', 'pandas.tests',
        'PIL.tests', 'IPython', 'jupyter', 'notebook',
        'pytest', 'unittest',
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
    name='watchdog-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX often triggers AV false positives
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,                    # keep console for now — easy debugging
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../frontend/build/icon.ico' if __import__('os').path.exists('../frontend/build/icon.ico') else None,
)
