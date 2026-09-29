# PyInstaller spec for the Windows build.
#
#   pip install pyinstaller
#   pyinstaller travelready.spec
#
# Produces dist/TravelReady.exe — a single windowed executable with no runtime
# dependencies beyond what Windows already provides.

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    datas=[("src/travelready/optimiser/data/profiles", "travelready/optimiser/data/profiles")],
    hiddenimports=["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "PIL", "matplotlib", "pandas", "pytest"],
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
    name="TravelReady",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed: the GUI is the default entry point
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
