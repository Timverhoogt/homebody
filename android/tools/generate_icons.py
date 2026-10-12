"""Regenerate the launcher icon layers from the dashboard's maskable PWA icon.

Usage (from the repository root, Pillow installed):  python3 android/tools/generate_icons.py

The output PNGs are committed, so a normal build does not need Python or Pillow.
"""
import pathlib
import sys

from PIL import Image, ImageDraw

ROOT = pathlib.Path(sys.argv[0]).resolve().parents[2]
SOURCE = ROOT / "homebody" / "static" / "icon-maskable-512.png"
RES = ROOT / "android" / "app" / "src" / "main" / "res" / "drawable-nodpi"
SIZE = 432  # 108 dp adaptive-icon layer at xxxhdpi


def main():
    icon = Image.open(SOURCE).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
    # Foreground: the full-bleed maskable artwork. The launcher mask shows only the central
    # 72/108 of the layer, which lies entirely inside the orange field around the robot.
    RES.mkdir(parents=True, exist_ok=True)
    icon.save(RES / "ic_launcher_foreground.png", optimize=True)
    # Monochrome (themed icons, Android 13+): the robot glyph as an alpha mask. The orange field
    # is found by flood fill so the orange eye inside the visor is kept; the dark visor is cut out.
    marked = icon.convert("RGB")
    ImageDraw.floodfill(marked, (SIZE // 10, SIZE // 2), (0, 255, 0), thresh=60)
    mono = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    src, flood, dst = icon.load(), marked.load(), mono.load()
    for y in range(SIZE):
        for x in range(SIZE):
            r, g, b, a = src[x, y]
            dark_visor = r + g + b < 150
            if a > 16 and flood[x, y] != (0, 255, 0) and not dark_visor:
                dst[x, y] = (255, 255, 255, 255)
    mono.save(RES / "ic_launcher_monochrome.png", optimize=True)


if __name__ == "__main__":
    main()
