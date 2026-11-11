"""Ponto de entrada da aplicação BrokCAM (interface gráfica)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path
from types import TracebackType

log = logging.getLogger("brokcam")


def log_dir() -> Path:
    from brokcam.ui.settings import config_dir
    d = config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging(level: int = logging.INFO) -> Path:
    """Log em arquivo rotativo (``%APPDATA%/BrokCAM/logs/brokcam.log``) + console."""
    path = log_dir() / "brokcam.log"
    root = logging.getLogger("brokcam")
    root.setLevel(logging.DEBUG)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)
    return path


def _show_crash_dialog(summary: str, details: str) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox
    if QApplication.instance() is None:
        return
    box = QMessageBox(QMessageBox.Icon.Critical, "BrokCAM — erro inesperado",
                      "Ocorreu um erro inesperado. O BrokCAM tentará continuar funcionando, "
                      "mas recomendamos salvar o projeto.\n\n" + summary)
    box.setInformativeText(f"Um relatório foi gravado em:\n{log_dir() / 'brokcam.log'}")
    box.setDetailedText(details)
    box.exec()


_in_hook = False


def excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    """Registra exceções não tratadas e mostra um diálogo amigável (sem derrubar o app)."""
    global _in_hook
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    log.critical("Exceção não tratada:\n%s", details)
    if _in_hook:
        return
    _in_hook = True
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and QThread.currentThread() is app.thread():
            _show_crash_dialog(f"{exc_type.__name__}: {exc}", details)
    except Exception:  # pragma: no cover
        pass
    finally:
        _in_hook = False


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    details = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    log.critical("Exceção não tratada na thread %s:\n%s", getattr(args.thread, "name", "?"), details)


def install_excepthooks() -> None:
    sys.excepthook = excepthook
    threading.excepthook = _thread_hook


def create_app(argv: list[str] | None = None):  # type: ignore[no-untyped-def]
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("BrokCAM")
    app.setOrganizationName("BrokCAM")
    app.setApplicationDisplayName("BrokCAM")
    from brokcam import __version__
    app.setApplicationVersion(__version__)
    from brokcam.ui.icons import app_icon
    app.setWindowIcon(app_icon())
    return app


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    setup_logging()
    install_excepthooks()
    if sys.platform == "win32":
        try:  # ícone próprio na barra de tarefas do Windows
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BrokCAM.App")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    app = create_app(argv)
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    win = MainWindow(AppSettings())
    win.show()
    files = [a for a in argv[1:] if os.path.exists(a)]
    if files:
        win.open_files(files)
    log.info("BrokCAM iniciado")
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
