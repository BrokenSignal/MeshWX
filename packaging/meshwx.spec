# PyInstaller spec — MeshWX standalone (onedir) build.
# Build from the repo root:   pyinstaller packaging/meshwx.spec
# Produces dist/MeshWX/ containing MeshWX(.exe) plus everything it needs.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Jinja templates must ship as data at the path routes.py expects.
datas = [(os.path.join(ROOT, "app", "web", "templates"),
          os.path.join("app", "web", "templates"))]
binaries = []
hiddenimports = collect_submodules("app")

# These libraries load submodules / data files dynamically, so sweep them whole.
for pkg in ("meshtastic", "meshcore", "uvicorn", "tzdata"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide6", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MeshWX",
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MeshWX",
)
