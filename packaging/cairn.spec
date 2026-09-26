# PyInstaller spec for dist/Cairn.app; packaging/build_app.sh runs it.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

import cairn

HERE = Path(SPECPATH)  # noqa: F821 - PyInstaller defines SPECPATH
VERSION = cairn.__version__

analysis = Analysis(  # noqa: F821
    [str(HERE / "app_main.py")],
    # the static web app, the example files, the plist template, and the metadata the
    # update check reads its repository from
    datas=collect_data_files("cairn") + copy_metadata("cairn-jobs"),
    # uvicorn loads its loop and protocol modules by name
    hiddenimports=collect_submodules("cairn") + collect_submodules("uvicorn"),
)
pyz = PYZ(analysis.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, analysis.scripts, [], exclude_binaries=True, name="Cairn",
    console=False, argv_emulation=False)
collected = COLLECT(exe, analysis.binaries, analysis.datas, name="Cairn")  # noqa: F821
app = BUNDLE(  # noqa: F821
    collected,
    name="Cairn.app",
    icon=str(HERE / "AppIcon.icns"),
    bundle_identifier="app.cairn.desktop",
    version=VERSION,
    info_plist={
        "CFBundleName": "Cairn",
        "CFBundleDisplayName": "Cairn",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": False,
        "NSHighResolutionCapable": True,
    },
)
