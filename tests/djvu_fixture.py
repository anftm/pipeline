"""Fixtures for native DjVu tests; never uses recognition or remote data."""
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw

HIDDEN_TEXT = '(page 0 0 400 600 (line 50 400 350 480 (word 50 400 150 480 "你好") (word 160 400 350 480 "world")))'


def build_djvu(directory: Path, rotation=0) -> Path:
    """Page 1: 600 dpi bitonal JB2 with hidden text; page 2: 150 dpi colour IW44."""
    bitonal = Image.new("1", (400, 600), 1)
    ImageDraw.Draw(bitonal).rectangle((50, 50, 350, 120), fill=0)
    bitonal.save(directory / "p1.pbm")
    colour = Image.new("RGB", (300, 200), (200, 30, 30))
    ImageDraw.Draw(colour).ellipse((50, 50, 250, 150), fill=(20, 200, 40))
    colour.save(directory / "p2.ppm")
    run = lambda *args: subprocess.run(args, check=True, capture_output=True, cwd=directory)
    run("cjb2", "-dpi", "600", "p1.pbm", "p1.djvu")
    run("c44", "-dpi", "150", "p2.ppm", "p2.djvu")
    run("djvm", "-c", "book.djvu", "p1.djvu", "p2.djvu")
    (directory / "t.txt").write_text(HIDDEN_TEXT, encoding="utf-8")
    run("djvused", "book.djvu", "-e", "select 1; set-txt t.txt", "-s")
    if rotation:
        run("djvused", "book.djvu", "-e", f"select 1; set-rotation {rotation}", "-s")
    return directory / "book.djvu"
