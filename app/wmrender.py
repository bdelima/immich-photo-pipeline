"""Draws the watermark on a photo, here, without Claude.

The text goes in a corner of the photo (the lower right unless another corner is
plainer), in up to three lines, each in its own font, colour and weight. Its size
follows the photo (a fraction of the shorter side), so it reads the same on a
phone snap and a 40-megapixel frame, and its colour, when left on "auto", is
picked from the brightness of the patch it sits on: white on dark, near-black on
light, always with a soft shadow and a faint highlight so it stands a little off
the photo.

Fonts: the three Liberation families the image ships with are always there. Any
other name is looked up on Google Fonts the first time and kept in a folder, so a
font Claude suggested is fetched once and drawn from then on.
"""
from __future__ import annotations

import io
import os
import re

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

INSTALLED = ("Liberation Sans", "Liberation Serif", "Liberation Mono")
DEFAULT_FONT = INSTALLED[0]
_LIBERATION_DIRS = ("/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2")
# Text height as a fraction of the photo's shorter side: the first line, then the others.
SIZE_FIRST = 0.032
SIZE_OTHER = 0.024
MIN_SIZE = 11
MARGIN = 0.025
GAP = 0.35                      # between lines, as a fraction of the line's size
LIGHT_TEXT = (255, 255, 255)
DARK_TEXT = (26, 26, 26)
AUTO_THRESHOLD = 140            # mean luminance above this gets dark text
CORNERS = ("lower right", "lower left", "upper right", "upper left")
GOOGLE_CSS = "https://fonts.googleapis.com/css2"
MAX_FONT_NAME = 60
FONT_TIMEOUT = 15
FONT_HOST = "https://fonts.gstatic.com/"          # where Google Fonts serves its files from
# The preview is drawn on a copy of the photo no larger than this on its longer side: the text is sized
# from the photo's shorter side, so it lays out the same, and a preview (redrawn as the form is edited)
# stays quick and light on memory even for a 40-megapixel frame.
PREVIEW_MAX = 1600
# The whole-photo picture behind the preview (the original with the watermark on it, shown when the
# preview is clicked) is drawn no larger than this on its longer side: it is for a screen, not a file.
WHOLE_MAX = 2000
# What a preview is drawn on when there is no original to draw on: a plain dark panel.
PLAIN_PANEL = (42, 47, 58)


class FontError(Exception):
    """The font can't be had (not installed, not on Google Fonts, or no network)."""


_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value) -> tuple[int, int, int] | None:
    """"#ff8c1a" -> (255, 140, 26); "white"/"black" -> those; "auto", "" or None -> None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in ("", "auto"):
        return None
    if v == "white":
        return LIGHT_TEXT
    if v == "black":
        return DARK_TEXT
    m = _HEX.match(v)
    if not m:
        return None
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _liberation_file(family: str, bold: bool, italic: bool) -> str | None:
    base = family.replace(" ", "")                        # LiberationSans
    style = ("Bold" if bold else "") + ("Italic" if italic else "") or "Regular"
    for d in _LIBERATION_DIRS:
        p = os.path.join(d, f"{base}-{style}.ttf")
        if os.path.isfile(p):
            return p
    return None


def _google_file(family: str, bold: bool, italic: bool, cache_dir: str) -> str:
    """The font file for a Google Fonts family in the wanted style, fetched once into
    `cache_dir`. Raises FontError if Google doesn't have it or it can't be reached."""
    if not family or len(family) > MAX_FONT_NAME or not re.match(r"^[A-Za-z0-9 .'&+-]+$", family):
        raise FontError(f"'{family}' is not a font name")
    want = f"{'1' if italic else '0'},{'700' if bold else '400'}"
    path = os.path.join(cache_dir, f"{_slug(family)}-{want.replace(',', '-')}.ttf")
    if os.path.isfile(path):
        return path
    os.makedirs(cache_dir, exist_ok=True)
    try:
        # With no browser user agent Google answers with plain TrueType files.
        r = requests.get(GOOGLE_CSS, params={"family": f"{family}:ital,wght@{want}"}, timeout=FONT_TIMEOUT,
                         headers={"User-Agent": "immich-photo-pipeline"})
    except requests.RequestException as exc:
        raise FontError(f"Google Fonts couldn't be reached: {exc}") from exc
    if r.status_code == 400:
        raise FontError(f"Google Fonts doesn't have '{family}'")
    if r.status_code != 200:
        raise FontError(f"Google Fonts answered {r.status_code}")
    urls = re.findall(r"src:\s*url\(([^)]+)\)", r.text)
    if not urls:
        raise FontError(f"Google Fonts doesn't have '{family}'")
    if not urls[0].strip("'\" ").startswith(FONT_HOST):
        raise FontError(f"Google Fonts pointed '{family}' somewhere unexpected")
    try:
        f = requests.get(urls[0].strip("'\" "), timeout=FONT_TIMEOUT)
        f.raise_for_status()
    except requests.RequestException as exc:
        raise FontError(f"the font file couldn't be fetched: {exc}") from exc
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(f.content)
    try:
        ImageFont.truetype(tmp, 20)
    except OSError:
        os.unlink(tmp)
        raise FontError(f"the file for '{family}' isn't a font")
    os.replace(tmp, path)
    return path


def font_file(family: str, bold: bool, italic: bool, cache_dir: str | None) -> tuple[str | None, bool]:
    """The file to draw `family` with, and whether it is the real style (False means the
    plain file was found and bold/italic must be faked). None means PIL's built-in font
    (only when Liberation isn't installed, as in a bare test box)."""
    family = (family or DEFAULT_FONT).strip()
    if family in INSTALLED:
        p = _liberation_file(family, bold, italic) or _liberation_file(family, False, False)
        return p, bool(p and p == _liberation_file(family, bold, italic))
    if cache_dir is None:
        raise FontError(f"'{family}' isn't installed")
    try:
        return _google_file(family, bold, italic, cache_dir), True
    except FontError:
        if bold or italic:
            return _google_file(family, False, False, cache_dir), False
        raise


def keep_font_file(family: str, src: str, cache_dir: str) -> str | None:
    """Copies a font file Claude fetched (at `src`) into the cache under `family`'s plain name,
    so the family can be drawn from now on. Returns the kept path, or None if `src` isn't a
    usable font file (nothing is kept then)."""
    if not family or len(family) > MAX_FONT_NAME or not os.path.isfile(src):
        return None
    try:
        ImageFont.truetype(src, 20)
    except OSError:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{_slug(family)}-0-400.ttf")
    if os.path.abspath(src) != os.path.abspath(path):
        with open(src, "rb") as fin, open(path + ".part", "wb") as fout:
            fout.write(fin.read())
        os.replace(path + ".part", path)
    return path


def check_font(family: str, cache_dir: str | None) -> None:
    """Raises FontError if `family` can't be drawn (fetching it if needed)."""
    font_file(family, False, False, cache_dir)


def _load(path: str | None, size: int):
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _luminance(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    region = img.crop(box).convert("L")
    if region.width == 0 or region.height == 0:
        return 0.0
    hist = region.histogram()
    total = sum(hist) or 1
    return sum(i * c for i, c in enumerate(hist)) / total


def _busyness(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    """How much is going on in a patch: the spread of its edges. A plain sky is near 0."""
    region = img.crop(box).convert("L")
    if region.width < 4 or region.height < 4:
        return 0.0
    region.thumbnail((160, 160))
    edges = region.filter(ImageFilter.FIND_EDGES)
    hist = edges.histogram()
    total = sum(hist) or 1
    mean = sum(i * c for i, c in enumerate(hist)) / total
    var = sum(((i - mean) ** 2) * c for i, c in enumerate(hist)) / total
    return var ** 0.5


def _layout(img: Image.Image, lines: list[dict], cache_dir: str | None, corner: str = "auto") -> dict:
    """Where everything goes: the font and size of each line, the block they make, and the
    corner it sits in (`corner` names one, or "auto" picks the plainest with the lower right
    preferred). Lines with no text are skipped."""
    short = min(img.width, img.height)
    margin = max(6, round(short * MARGIN))
    drawn = []
    for i, ln in enumerate(lines):
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        size = max(MIN_SIZE, round(short * (SIZE_FIRST if i == 0 else SIZE_OTHER)))
        path, real = font_file(ln.get("font") or DEFAULT_FONT, bool(ln.get("bold")), bool(ln.get("italic")), cache_dir)
        font = _load(path, size)
        fake_bold = bool(ln.get("bold")) and not real
        fake_italic = bool(ln.get("italic")) and not real
        stroke = max(1, size // 28) if fake_bold else 0
        l, t, r, b = font.getbbox(text, stroke_width=stroke)
        w, h = r - l, b - t
        if fake_italic:
            w += int(h * 0.2)
        drawn.append({"text": text, "font": font, "size": size, "w": w, "h": h, "offset": (l, t), "stroke": stroke,
                      "fake_italic": fake_italic, "color": parse_color(ln.get("color")), "index": i})
    if not drawn:
        return {"lines": [], "corner": corner if corner in CORNERS else CORNERS[0], "box": (0, 0, 0, 0)}
    block_w = max(d["w"] for d in drawn)
    block_h = sum(d["h"] for d in drawn) + sum(round(d["size"] * GAP) for d in drawn[:-1])
    pad = margin // 2
    boxes = {
        "lower right": (img.width - margin - block_w - pad, img.height - margin - block_h - pad, img.width - margin + pad, img.height - margin + pad),
        "lower left": (margin - pad, img.height - margin - block_h - pad, margin + block_w + pad, img.height - margin + pad),
        "upper right": (img.width - margin - block_w - pad, margin - pad, img.width - margin + pad, margin + block_h + pad),
        "upper left": (margin - pad, margin - pad, margin + block_w + pad, margin + block_h + pad),
    }
    clamp = lambda b: (max(0, b[0]), max(0, b[1]), min(img.width, b[2]), min(img.height, b[3]))
    if corner not in CORNERS:
        scores = {c: _busyness(img, clamp(b)) for c, b in boxes.items()}
        # The lower right wins unless another corner is clearly plainer (a third less going on).
        corner = CORNERS[0]
        best = scores[corner]
        for c in CORNERS[1:]:
            if scores[c] < best * 0.66:
                corner, best = c, scores[c]
    return {"lines": drawn, "corner": corner, "box": clamp(boxes[corner]), "block": (block_w, block_h), "margin": margin}


def _draw_block(img: Image.Image, lay: dict) -> list[tuple[int, int, int]]:
    """Draws the lines into `img` (RGBA work is done on a layer). Returns the colour used per line."""
    if not lay["lines"]:
        return []
    x0, y0, x1, y1 = lay["box"]
    right = lay["corner"].endswith("right")
    lum = _luminance(img, (x0, y0, x1, y1))
    auto = DARK_TEXT if lum > AUTO_THRESHOLD else LIGHT_TEXT
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld, sd = ImageDraw.Draw(layer), ImageDraw.Draw(shadow)
    block_w, block_h = lay["block"]
    margin = lay["margin"]
    y = (img.height - margin - block_h) if lay["corner"].startswith("lower") else margin
    used = []
    for d in lay["lines"]:
        color = d["color"] or auto
        used.append(color)
        x = (img.width - margin - d["w"]) if right else margin
        ox, oy = d["offset"]
        pos = (x - ox, y - oy)
        k = max(1, d["size"] // 18)
        dark = sum(color) < 384
        shade = (0, 0, 0, 170) if not dark else (255, 255, 255, 110)
        sd.text((pos[0] + k, pos[1] + k), d["text"], font=d["font"], fill=shade, stroke_width=d["stroke"])
        # A faint highlight on the other side makes it stand off the photo a little.
        hi = (255, 255, 255, 70) if not dark else (0, 0, 0, 60)
        ld.text((pos[0] - 1, pos[1] - 1), d["text"], font=d["font"], fill=hi, stroke_width=d["stroke"])
        ld.text(pos, d["text"], font=d["font"], fill=color + (255,), stroke_width=d["stroke"], stroke_fill=color + (255,))
        y += d["h"] + round(d["size"] * GAP)
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, lay["lines"][0]["size"] / 14)))
    # Faked italics: shear the text layers a little.
    if any(d["fake_italic"] for d in lay["lines"]):
        sh, yc = 0.2, (y0 + y1) / 2
        layer = layer.transform(layer.size, Image.AFFINE, (1, sh, -sh * yc, 0, 1, 0))
        shadow = shadow.transform(shadow.size, Image.AFFINE, (1, sh, -sh * yc, 0, 1, 0))
    base = img.convert("RGBA")
    base.alpha_composite(shadow)
    base.alpha_composite(layer)
    img.paste(base.convert(img.mode))
    return used


def render(src_path: str, dest_path: str, lines: list[dict], cache_dir: str | None, corner: str = "auto") -> dict:
    """Writes the photo at `src_path` with the watermark to `dest_path` (JPEG, high quality).
    `lines` is up to three {text, font, color, bold, italic}. Returns {"corner", "colors"}."""
    with Image.open(src_path) as im:
        # Upright first: the saved JPEG carries no orientation tag, so a photo that only
        # looked upright because of its tag (an imported phone picture) would come out sideways.
        img = ImageOps.exif_transpose(im).convert("RGB")
    lay = _layout(img, lines, cache_dir, corner)
    if not lay["lines"]:
        raise ValueError("there is nothing to write")
    colors = _draw_block(img, lay)
    img.save(dest_path, "JPEG", quality=94, subsampling=0)
    return {"corner": lay["corner"], "colors": ["#%02x%02x%02x" % c for c in colors]}


def _reduced(src_path: str, longest: int) -> Image.Image:
    """The photo upright, in RGB, no larger than `longest` on its longer side."""
    with Image.open(src_path) as im:
        im.draft("RGB", (longest, longest))               # a JPEG is decoded at a reduced size
        img = ImageOps.exif_transpose(im).convert("RGB")
    img.thumbnail((longest, longest))
    return img


def preview(src_path: str | None, lines: list[dict], cache_dir: str | None, width: int = 560, height: int = 180,
            corner: str = "auto") -> tuple[bytes, dict]:
    """A PNG of the corner the text will sit on, with the text drawn by the same code, scaled to
    `width` x `height`. Returns (png bytes, {"corner", "colors"}). With nothing to write it shows
    the corner plain. With no photo (`src_path` None) the text is drawn on a plain dark panel of
    the preview's shape, so the font and colour can still be seen."""
    if src_path is None:
        img = Image.new("RGB", (PREVIEW_MAX, int(PREVIEW_MAX * height / width)), PLAIN_PANEL)
    else:
        img = _reduced(src_path, PREVIEW_MAX)
    lay = _layout(img, lines, cache_dir, corner)
    colors = _draw_block(img, lay) if lay["lines"] else []
    # The window: the block plus room around it, in the preview's shape, scaled so the text is
    # readable; it never goes outside the photo.
    short = min(img.width, img.height)
    win_h = max(int(short * 0.22), (lay["box"][3] - lay["box"][1]) + int(short * 0.06)) if lay["lines"] else int(short * 0.22)
    win_w = int(win_h * width / height)
    if win_w > img.width:
        win_w = img.width
        win_h = int(win_w * height / width)
    corner = lay["corner"]
    x1 = img.width if corner.endswith("right") else win_w
    y1 = img.height if corner.startswith("lower") else win_h
    crop = img.crop((x1 - win_w, y1 - win_h, x1, y1)).resize((width, height), Image.LANCZOS)
    out = io.BytesIO()
    crop.save(out, "PNG")
    return out.getvalue(), {"corner": corner, "colors": ["#%02x%02x%02x" % c for c in colors]}


def whole(src_path: str, lines: list[dict], cache_dir: str | None, corner: str = "auto") -> tuple[bytes, dict]:
    """The whole photo with the watermark on it, as a JPEG for the screen (no larger than
    WHOLE_MAX on its longer side). Returns (jpeg bytes, {"corner", "colors"}). Nothing is written."""
    img = _reduced(src_path, WHOLE_MAX)
    lay = _layout(img, lines, cache_dir, corner)
    colors = _draw_block(img, lay) if lay["lines"] else []
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue(), {"corner": lay["corner"], "colors": ["#%02x%02x%02x" % c for c in colors]}
