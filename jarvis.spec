# PyInstaller build for Jarvis - a windowless folder build (dist\Jarvis\).
#
#   scripts\build_exe.ps1            (installs PyInstaller, builds, self-tests)
#   python -m PyInstaller jarvis.spec --noconfirm --clean
#
# A folder build (onedir) rather than one file: PyTorch for the speech models
# is ~570 MB, and a one-file exe would unpack all of it to a temp folder on
# every start. Data never lives in the build folder (see app_paths.py), so a
# rebuild can replace dist\Jarvis\ safely. The .env file is NOT bundled - the
# API key stays in %LOCALAPPDATA%\Jarvis\.env.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("ui", "ui"),                          # HUD page + SVG
    ("browser_tabs.ps1", "."),             # browser tab control
]
# openWakeWord's ONNX models (mel spectrogram, embedding, "hey jarvis")
datas += collect_data_files("openwakeword")
# Playwright's driver (node.exe + JS) for WhatsApp Web. The browser itself
# is the installed Chrome/Edge, so nothing else is bundled.
datas += collect_data_files("playwright")

hiddenimports = (
    # transformers loads model classes lazily by name
    collect_submodules("transformers.models.whisper")
    + ["win32timezone"]                    # pywin32, used by win32com at runtime
    # pycaw/comtypes build COM interfaces at runtime, and
    # screen-brightness-control picks its backend by platform
    + collect_submodules("pycaw")
    + collect_submodules("screen_brightness_control")
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # not used by Jarvis; keeps the build smaller
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest", "tests",
              "torchvision", "torchaudio", "tensorboard"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Jarvis",
    icon="assets/jarvis.ico",           # also the Desktop shortcut's icon
    console=False,                         # windowless; output goes to the log file
    upx=False,                             # UPX-packed exes trip antivirus heuristics
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Jarvis",
)
