# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the WATCH-DOG FastAPI backend.
# Run:  pyinstaller backend.spec --clean
#
# Produces a folder dist/watchdog-backend/ containing watchdog-backend.exe
# plus all DLLs/datas alongside it (--onedir mode).
#
# Why --onedir not --onefile: --onefile bundles everything into a single exe
# that, when launched, EXTRACTS itself to %TEMP%, then spawns the REAL
# Python process as a child. Result: 2 entries per Python service in
# Task Manager (bootloader + extracted process). Users (correctly) see
# this as confusing duplicate processes.
#
# --onedir ships the unpacked contents alongside the exe up front. No
# extraction at launch, no bootloader child process. Task Manager shows
# exactly ONE entry per service. Trade-off: install footprint is the same
# total size, just spread across many files instead of one big exe.
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
    # cloud_client is imported at top of routers.bots so static analysis
    # picks it up — but list explicitly so a refactor that moves the
    # import inside a function doesn't silently lose it from the bundle.
    'app.cloud_client',
    # sync_engine is imported lazily inside main.py's lifespan handler.
    # PyInstaller's static analyser does NOT see lazy/in-function imports,
    # so this MUST be explicit or sync_engine.py won't be in the exe and
    # cloud-sync silently degrades to local-only at every startup.
    'app.sync_engine',
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

# sdk/ — bot runtime helpers (wd_runner.py + wd_autolog + wd_log + wd_session).
# These are loaded by os.path.join from app/routers/bots.py, NOT via Python
# import, so PyInstaller's static analysis misses them. Without this, every
# bot launch fails because subprocess can't find wd_runner.py.
datas += [('sdk', 'sdk')]


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
    [],
    exclude_binaries=True,            # --onedir: binaries go in COLLECT, not the exe
    name='watchdog-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                        # UPX often triggers AV false positives
    console=True,                     # keep console for now — easy debugging
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../frontend/build/icon.ico' if __import__('os').path.exists('../frontend/build/icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='watchdog-backend',          # output folder: dist/watchdog-backend/
)
