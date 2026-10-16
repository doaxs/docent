# PyInstaller spec for the Docent macOS app.
# Build: pyinstaller Docent.spec
# Output: dist/Docent.app

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("samsungtvws")
    + collect_submodules("uvicorn")
    + ["app.main"]
)

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
]

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Docent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Docent",
)

app = BUNDLE(
    coll,
    name="Docent.app",
    icon="assets/AppIcon.icns",
    bundle_identifier="io.github.doaxs.docent",
    info_plist={
        "CFBundleName": "Docent",
        "CFBundleDisplayName": "Docent",
        "CFBundleShortVersionString": "1.0.1",
        "CFBundleVersion": "1.0.1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
    },
)
