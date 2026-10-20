"""Render the nour-code app icon and social card (PNG) from their geometry.

The SVG in ``landing/public/icon.svg`` is the source of truth for the shape;
this script draws the same geometry with Pillow so the raster versions
(favicon, apple-touch-icon, 512 px store icon) stay pixel-identical without
needing cairo/librsvg on the build machine.

    python scripts/make-icon.py          # writes into landing/public/

Design: a dark rounded square, an "N" drawn as three round-capped strokes,
filled with a lime-to-blue gradient (the landing page is lime, the console is
blue). It has to read as a letter at 16 px, so nothing else is in the mark.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (11, 13, 18, 255)
EDGE = (35, 42, 58, 255)
LIME = (217, 255, 99)
BLUE = (108, 140, 255)

S = 512           # logical size
SS = 4            # supersampling factor
STROKE = 58       # stroke width, logical px
RADIUS = 116      # corner radius

# the N: left stem, diagonal, right stem (logical coordinates)
P_LEFT_TOP = (150, 158)
P_LEFT_BOTTOM = (150, 354)
P_RIGHT_TOP = (362, 158)
P_RIGHT_BOTTOM = (362, 354)

OUT = Path(__file__).resolve().parent.parent / "public"


def gradient(size: int) -> Image.Image:
    """Diagonal lime -> blue gradient."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x / size * 0.75) + (y / size * 0.25)
            r = round(LIME[0] + (BLUE[0] - LIME[0]) * t)
            g = round(LIME[1] + (BLUE[1] - LIME[1]) * t)
            b = round(LIME[2] + (BLUE[2] - LIME[2]) * t)
            px[x, y] = (r, g, b)
    return img


def render(size: int) -> Image.Image:
    big = size * SS
    k = big / S  # logical -> pixels

    icon = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=RADIUS * k, fill=BG)

    # strokes go on a mask so the gradient can be composited through them
    mask = Image.new("L", (big, big), 0)
    pen = ImageDraw.Draw(mask)
    width = round(STROKE * k)
    for a, b in ((P_LEFT_TOP, P_LEFT_BOTTOM), (P_LEFT_TOP, P_RIGHT_BOTTOM), (P_RIGHT_TOP, P_RIGHT_BOTTOM)):
        pen.line([(a[0] * k, a[1] * k), (b[0] * k, b[1] * k)], fill=255, width=width)
    # round caps and joins
    for point in (P_LEFT_TOP, P_LEFT_BOTTOM, P_RIGHT_TOP, P_RIGHT_BOTTOM):
        r = width / 2
        pen.ellipse((point[0] * k - r, point[1] * k - r, point[0] * k + r, point[1] * k + r), fill=255)

    icon.paste(gradient(big), (0, 0), mask)

    # hairline inner edge: keeps the tile crisp on light backgrounds
    draw.rounded_rectangle(
        (3 * k, 3 * k, big - 3 * k, big - 3 * k),
        radius=(RADIUS - 3) * k,
        outline=EDGE,
        width=max(1, round(6 * k)),
    )

    return icon.resize((size, size), Image.LANCZOS)


FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONT_DIR / name
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:  # pragma: no cover - any DejaVu-less machine
        return ImageFont.load_default()


def render_og() -> Image.Image:
    """The 1200x630 social card: the mark, the name, one honest sentence."""
    width, height = 1200, 630
    card = Image.new("RGB", (width, height), BG[:3])
    px = card.load()
    # a very soft vertical wash so the flat background does not band
    for y in range(height):
        t = y / height
        px_row = (round(11 + 4 * t), round(13 + 6 * t), round(18 + 12 * t))
        for x in range(width):
            px[x, y] = px_row

    mark = render(184)
    card.paste(mark, (92, 223), mark)

    draw = ImageDraw.Draw(card)
    draw.text((330, 214), "nour-code", font=font("DejaVuSans-Bold.ttf", 92), fill=(238, 242, 250))
    draw.text(
        (334, 330),
        "a coding agent that stays on your machine",
        font=font("DejaVuSans.ttf", 34),
        fill=(151, 161, 181),
    )
    draw.text(
        (334, 392),
        "Qwen3 8B on Ollama · FastAPI · seven audited tools",
        font=font("DejaVuSans.ttf", 28),
        fill=(108, 140, 255),
    )

    # a lime rule under the text, echoing the icon's gradient start
    draw.rounded_rectangle((334, 466, 470, 471), radius=3, fill=LIME)
    draw.text(
        (334, 500),
        "nothing leaves your machine",
        font=font("DejaVuSansMono.ttf", 24),
        fill=(217, 255, 99),
    )
    draw.text(
        (width - 92, 552),
        "nour-code-agent.vercel.app",
        font=font("DejaVuSansMono.ttf", 22),
        fill=(92, 101, 122),
        anchor="rs",
    )
    return card


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    og = render_og()
    og.save(OUT / "og.png", optimize=True)
    print(f"wrote {OUT / 'og.png'} (1200x630)")
    OUT.mkdir(parents=True, exist_ok=True)
    for size, name in ((512, "icon-512.png"), (180, "apple-touch-icon.png"), (64, "icon-64.png"), (32, "favicon-32.png")):
        render(size).save(OUT / name, optimize=True)
        print(f"wrote {OUT / name} ({size}x{size})")
    # multi-size favicon
    sizes = [(16, 16), (32, 32), (48, 48)]
    render(64).save(OUT / "favicon.ico", sizes=sizes)
    print(f"wrote {OUT / 'favicon.ico'} {sizes}")


if __name__ == "__main__":
    main()
