# The Ada app icon, drawn with Pillow (system python3). Regenerate with
#   python3 gen_app_icon.py && npx tauri icon src-tauri/icons/app-icon.png
# from app/ (the second command writes every platform size), then copy the
# 512 px render to src/assets/kaleo-mark.png and site/assets/icon.png with
# --mark (see main()).
#
# Geometry follows Apple's macOS app icon template: a 1024 px canvas with an
# 824 px rounded-square tile centred on it (100 px margin), corner radius 185.
# The mark is a differential pair drawn the way the router draws copper: two
# traces of one width, a 45-degree jog, spacing equal to the
# trace width -- the tightly coupled pair signals.py writes a net class for --
# copper on a near-black tile (2026-09-16; before this the mark was a copper
# "A" on solder-mask green). Shapes are rendered 4x supersampled and
# downscaled, so edges are anti-aliased.
import sys

from PIL import Image, ImageDraw

SS = 4
SIZE = 1024 * SS
TILE = 824 * SS
RADIUS = 185 * SS
TILE_FILL = (10, 10, 10, 255)
TILE_EDGE = (28, 28, 28, 255)
COPPER = (214, 140, 72, 255)

# Trace geometry on the 1024 canvas, centred on (512, 512). Width 80; the pair
# pitch is 128, so the gap between the two conductors equals the trace width.
# The pitch is measured perpendicular to every segment, slant included: the
# inner trace's two bends move along X by pitch * tan(22.5 deg), the mitre a
# coupled router draws (engine/silkscreen/diffpair.py). Offsetting the whole
# trace straight down instead left the slant 1/sqrt(2) as wide (caught by Pat,
# 2026-09-16).
WIDTH = 80
TRACE_P = [(168, 572), (352, 572), (600, 324), (856, 324)]
TRACE_N = [(168, 700), (405.0, 700), (653.0, 452), (856, 452)]


def s(v: float) -> int:
    return int(round(v * SS))


def draw(tile_box: tuple[int, int, int, int], radius: int, scale: float) -> Image.Image:
    """The tile and the pair, at ``scale`` times the 1024 geometry, supersampled."""
    canvas = int(round(1024 * scale * SS))
    im = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = (int(round(v * scale * SS)) for v in tile_box)
    r = int(round(radius * scale * SS))
    d.rounded_rectangle([x0, y0, x1, y1], r, fill=TILE_EDGE)
    inset = int(round(6 * scale * SS))
    d.rounded_rectangle([x0 + inset, y0 + inset, x1 - inset, y1 - inset], r - inset, fill=TILE_FILL)
    width = int(round(WIDTH * scale * SS))
    for path in (TRACE_P, TRACE_N):
        pts = [(int(round(x * scale * SS)), int(round(y * scale * SS))) for x, y in path]
        d.line(pts, fill=COPPER, width=width, joint="curve")
        for x, y in (pts[0], pts[-1]):
            rr = width // 2
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=COPPER)
    return im


def main() -> None:
    # The macOS icon: tile with the template's margin on a 1024 canvas.
    o = (1024 - 824) // 2
    draw((o, o, o + 824, o + 824), 185, 1.0).resize((1024, 1024), Image.LANCZOS).save("app-icon.png")
    if "--mark" in sys.argv:
        # The in-app and site mark: the tile fills the whole 512 square, so
        # CSS rounding (`rounded-2xl`, `rounded-lg`) lands on the tile edge.
        # Same geometry, re-centred to the tile.
        im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, SIZE, SIZE], int(1024 * SS * 110 / 512), fill=TILE_FILL)
        k = 1024 / 824  # the traces were placed inside the 824 tile; stretch to 1024
        for path in (TRACE_P, TRACE_N):
            pts = [(s((x - o) * k), s((y - o) * k)) for x, y in path]
            w = s(WIDTH * k)
            d.line(pts, fill=COPPER, width=w, joint="curve")
            for x, y in (pts[0], pts[-1]):
                rr = w // 2
                d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=COPPER)
        out = im.resize((512, 512), Image.LANCZOS)
        out.save("../../src/assets/kaleo-mark.png")
        out.save("../../../site/assets/icon.png")


if __name__ == "__main__":
    main()
