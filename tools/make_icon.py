"""Render the Theara SVG mark to a Windows ICO file for packaging."""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "assets" / "theara.svg"
ICO_PATH = ROOT / "assets" / "theara.ico"
PNG_PATH = ROOT / "assets" / "theara.png"


def main() -> int:
    renderer = QSvgRenderer(str(SVG_PATH))
    if not renderer.isValid():
        raise RuntimeError(f"Could not read {SVG_PATH.name}.")

    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(PNG_PATH), "PNG"):
        raise RuntimeError("Qt could not create the preview icon.")
    with Image.open(PNG_PATH) as source:
        source.save(
            ICO_PATH,
            format="ICO",
            sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)],
        )
    print(ICO_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
