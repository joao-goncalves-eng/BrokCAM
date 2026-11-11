"""Gera o ícone do aplicativo (``brokcam/ui/assets``) a partir dos SVGs.

    .venv\\Scripts\\python.exe tools\\make_app_icon.py

* ``brokcam.svg`` (mestre) → tamanhos ≥ 32 px;
* ``brokcam-small.svg`` (B maior, sem detalhes finos) → 16, 20 e 24 px;
* grava ``brokcam.ico`` (todos os tamanhos, PNG dentro do ICO — Windows Vista+) e
  ``brokcam-256.png`` (README / atalhos). Só depende do PySide6.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "brokcam" / "ui" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SMALL_MAX = 24


def render(svg: Path, size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    QSvgRenderer(str(svg)).render(p)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        s = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.write_bytes(header + entries + blobs)


def main() -> int:
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    big, small = ASSETS / "brokcam.svg", ASSETS / "brokcam-small.svg"
    images = [(s, png_bytes(render(small if s <= SMALL_MAX else big, s))) for s in SIZES]
    write_ico(ASSETS / "brokcam.ico", images)
    (ASSETS / "brokcam-256.png").write_bytes(images[-1][1])
    print(f"ok: {ASSETS / 'brokcam.ico'} ({', '.join(str(s) for s in SIZES)} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
