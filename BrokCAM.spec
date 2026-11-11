# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller para o executável do BrokCAM (pasta dist/BrokCAM, com BrokCAM.exe).
# Não rode direto: use GerarExecutavel.bat (ou tools/build_exe.ps1), que também testa o resultado.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (ROOT / "brokcam" / "__init__.py").read_text(encoding="utf-8")).group(1)
nums = [int(x) for x in re.findall(r"\d+", VERSION)[:3]] + [0]
while len(nums) < 4:
    nums.append(0)

# recurso de versão do Windows (Propriedades › Detalhes do .exe)
BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)
version_file = BUILD / "version_info.txt"
version_file.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(nums)}, prodvers={tuple(nums)}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041604B0', [
      StringStruct('CompanyName', 'BrokCAM'),
      StringStruct('FileDescription', 'BrokCAM - CAM para PCB com GRBL'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'BrokCAM'),
      StringStruct('LegalCopyright', 'Licença MIT'),
      StringStruct('OriginalFilename', 'BrokCAM.exe'),
      StringStruct('ProductName', 'BrokCAM'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1046, 1200])])
  ]
)
""", encoding="utf-8")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "brokcam" / "ui" / "icons"), "brokcam/ui/icons"),     # ícones da interface
        (str(ROOT / "brokcam" / "ui" / "assets"), "brokcam/ui/assets"),   # ícone do aplicativo
        (str(ROOT / "tests" / "data"), "tests/data"),                     # "Abrir exemplo"
    ],
    # o backend importa parsers/cam/gcode/machine dinamicamente (importlib): incluir todos
    hiddenimports=collect_submodules("brokcam") + ["PySide6.QtSvg"],
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "PyQt5", "PyQt6", "PySide2", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="BrokCAM",
    console=False,                      # app de janela (sem console preto)
    icon=str(ROOT / "brokcam" / "ui" / "assets" / "brokcam.ico"),
    version=str(version_file),
    upx=False,                          # UPX aumenta alarmes falsos de antivírus
)
coll = COLLECT(exe, a.binaries, a.datas, name="BrokCAM", upx=False)
