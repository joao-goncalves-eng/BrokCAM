"""Inicia o BrokCAM.

Uso::

    .venv\\Scripts\\python.exe run.py [arquivos...]

Erros não tratados são gravados em ``%APPDATA%/BrokCAM/logs/brokcam.log`` e
exibidos num diálogo amigável em vez de fechar o programa.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fatal(exc: BaseException) -> int:
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_path = None
    try:
        from brokcam.app import log_dir
        log_path = log_dir() / "brokcam-crash.log"
        log_path.write_text(details, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.stderr.write(details)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox(QMessageBox.Icon.Critical, "BrokCAM não pôde iniciar",
                          f"Erro ao iniciar o BrokCAM:\n\n{type(exc).__name__}: {exc}")
        if log_path:
            box.setInformativeText(f"Detalhes gravados em:\n{log_path}")
        box.setDetailedText(details)
        box.exec()
        del app
    except Exception:  # noqa: BLE001
        pass
    return 1


def main() -> int:
    try:
        from brokcam.app import main as app_main
        return app_main(sys.argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001
        return _fatal(exc)


if __name__ == "__main__":
    sys.exit(main())
