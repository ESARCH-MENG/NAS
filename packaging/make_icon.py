"""Generate the macOS NAS icon using Storm's artwork fitting approach."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PROJECT_ROOT / "nas icon.png"
DEFAULT_OUTPUT = PROJECT_ROOT / "packaging" / "assets" / "app_icon.icns"
CANVAS = 1024
BODY = 824
CORNER_RATIO = 0.2248
PROBE = 256


def build_master(source: Image.Image) -> Image.Image:
    image = source.convert("RGBA")
    width, height = image.size
    side = min(width, height)
    left, top = (width - side) // 2, (height - side) // 2
    square = image.crop((left, top, left + side, top + side))
    if side < CANVAS:
        print(f"Note: source is {side}px; upscaling to {CANVAS}px.")
    small = square.resize((64, 64))
    if any(small.getpixel(point)[3] < 32 for point in ((0, 0), (63, 0), (0, 63), (63, 63))):
        return square.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)

    body = square.resize((BODY, BODY), Image.Resampling.LANCZOS)
    probe = square.convert("RGB").resize((PROBE, PROBE))
    background = probe.getpixel((0, 0))
    difference = ImageChops.difference(
        probe, Image.new("RGB", probe.size, background)
    )
    content = difference.convert("L").point(lambda value: 255 if value > 16 else 0)
    bounds = content.getbbox()
    background_fraction = 1 - sum(content.histogram()[1:]) / (PROBE * PROBE)
    if bounds is not None and background_fraction >= 0.30:
        radius = round(BODY * CORNER_RATIO)
        low, high = 0.5, 1.0
        for _ in range(48):
            scale = (low + high) / 2
            offset = BODY * (1 - scale) / 2
            fits = True
            for x, y in (
                (bounds[0], bounds[1]), (bounds[2], bounds[1]),
                (bounds[0], bounds[3]), (bounds[2], bounds[3]),
            ):
                x = offset + scale * BODY * x / PROBE
                y = offset + scale * BODY * y / PROBE
                core_x = min(max(x, radius), BODY - radius)
                core_y = min(max(y, radius), BODY - radius)
                if (
                    min(x, y, BODY - x, BODY - y) < 8
                    or (x - core_x) ** 2 + (y - core_y) ** 2 > (radius - 8) ** 2
                ):
                    fits = False
                    break
            if fits:
                low = scale
            else:
                high = scale
        if low < 0.999:
            size = round(BODY * low)
            shrunk = body.resize((size, size), Image.Resampling.LANCZOS)
            body = Image.new("RGBA", (BODY, BODY), (*background, 255))
            body.paste(shrunk, ((BODY - size) // 2, (BODY - size) // 2))

    mask = Image.new("L", (BODY * 2, BODY * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, BODY * 2 - 1, BODY * 2 - 1),
        radius=round(BODY * 2 * CORNER_RATIO),
        fill=255,
    )
    mask = mask.resize((BODY, BODY), Image.Resampling.LANCZOS)
    master = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    offset = (CANVAS - BODY) // 2
    master.paste(body, (offset, offset), mask)
    return master


def generate_icon(source: Path, output: Path) -> None:
    with Image.open(source) as image:
        master = build_master(image)
    output.parent.mkdir(parents=True, exist_ok=True)
    iconutil = shutil.which("iconutil")
    with tempfile.TemporaryDirectory(prefix="nas-icon-") as temporary:
        temporary_root = Path(temporary)
        generated = temporary_root / "app_icon.icns"
        if iconutil:
            iconset = temporary_root / "app_icon.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                for factor, suffix in ((1, ""), (2, "@2x")):
                    master.resize(
                        (size * factor, size * factor), Image.Resampling.LANCZOS
                    ).save(iconset / f"icon_{size}x{size}{suffix}.png")
            subprocess.run(
                [iconutil, "-c", "icns", str(iconset), "-o", str(generated)],
                check=True,
            )
        else:
            master.save(generated, format="ICNS")
        with Image.open(generated) as result:
            result.load()
            if result.format != "ICNS" or result.size != (CANVAS, CANVAS):
                raise ValueError("Generated icon is not a valid 1024px ICNS.")
        shutil.copyfile(generated, output)
    print(f"Icon written: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    generate_icon(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
