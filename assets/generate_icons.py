#!/usr/bin/env python3
"""Generate orient's home-screen / favicon icons: a code-drawn mark on orient's
brand gradient, no raster source involved.

Geometry measured directly from Convivy's own reference mark
(Convivy_Icons_Palm Green-04.png, provided by the operator): a disc with a 90-degree
wedge cut out (the mouth, forming a "C" open to the right) plus a counter-
circle CENTRED ON THE DISC CENTRE that rounds the inner corner where the
wedge converges. Drawing the geometry in code (Pillow ImageDraw, supersampled
for antialiasing) means the mark is a set of parameters instead of pixels
baked into source-mark.png.

Unlike Deck (a macOS .icns, inset in a rounded plate with margin for the
system's shadow/reflection), orient's icons are full-bleed square PWA/iOS
home-screen icons: the gradient fills every pixel edge-to-edge so iOS applies
its own mask cleanly instead of matting transparent corners black. The mark
disc radius (0.34) and other ratios below are fractions of that full square,
not of an inset "art" rect.

Run:  /opt/homebrew/bin/python3.11 assets/generate_icons.py
Commit the PNGs + site.webmanifest it writes alongside this script.
"""
import json
import math
import pathlib

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent

# Gradient stops (design_system.py --grad, light theme:
# linear-gradient(120deg, #E8542F 0%, #FF7A3D 52%, #FFA64D 100%)).
STOPS = [(0.0, (232, 84, 47)), (0.52, (255, 122, 61)), (1.0, (255, 166, 77))]
ANGLE_DEG = 120

MARK_COLOR = (255, 255, 255)

# Geometry ratios measured from Convivy_Icons_Palm Green-04.png, relative to
# the full canvas. That reference measured disc_radius=172.5px on a 600px
# canvas (0.2875 of the canvas) with mouth_half_angle=45.0deg (the wedge
# boundary line passes almost exactly through the disc centre) and a
# counter-circle CENTRED on the disc centre (measured offset ~1px, i.e. noise)
# with radius 33.0px, a 0.191 ratio of the disc radius. Orient keeps its own
# disc-radius ratio (0.34, already tuned for the full-bleed square icon) but
# takes the wedge angle and counter-circle centring from the reference.
DISC_RADIUS_RATIO = 0.34
MOUTH_HALF_ANGLE_DEG = 45
# Reference baseline is 0.191 (of disc radius) — grown a little per the operator's
# note ("narrow the C a little bit, not too much"), not the much larger jump
# tried previously.
COUNTER_RADIUS_RATIO = 0.24
COUNTER_OFFSET_RATIO = 0.0  # centred on the disc centre, per the reference

SUPERSAMPLE = 4
MIN_SUPERSAMPLE_PX = 256  # floor so small sizes (e.g. the 32px favicon) still antialias cleanly


def grad_color(t):
    t = max(0.0, min(1.0, t))
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(round(c0[i] + (c1[i] - c0[i]) * f) for i in range(3))
    return STOPS[-1][1]


def build_gradient_image(size):
    """The gradient is a smooth continuous function, so it's computed exactly
    at the target size — no supersampling needed, unlike the mark geometry."""
    theta = math.radians(ANGLE_DEG)
    dx, dy = math.sin(theta), -math.cos(theta)
    projs = [x * dx + y * dy for x in (0, size) for y in (0, size)]
    pmin, pmax = min(projs), max(projs)
    span = (pmax - pmin) or 1.0

    img = Image.new("RGB", (size, size))
    put = img.load()
    for y in range(size):
        for x in range(size):
            put[x, y] = grad_color((x * dx + y * dy - pmin) / span)
    return img


def build_mark_mask(size):
    """White-on-black alpha mask of the mark (disc minus mouth wedge, minus
    counter-circle), rendered supersampled then downsampled for antialiasing."""
    ss = max(size * SUPERSAMPLE, MIN_SUPERSAMPLE_PX)
    mask = Image.new("L", (ss, ss), 0)
    draw = ImageDraw.Draw(mask)

    cx, cy = ss / 2, ss / 2
    radius = ss * DISC_RADIUS_RATIO
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    # Pillow's pieslice sweeps clockwise from start to end; leaves the
    # (2 * MOUTH_HALF_ANGLE_DEG)-degree wedge centered on the positive x-axis
    # (3 o'clock) open — the mouth, opening to the right.
    draw.pieslice(bbox, start=MOUTH_HALF_ANGLE_DEG, end=360 - MOUTH_HALF_ANGLE_DEG, fill=255)

    counter_radius = radius * COUNTER_RADIUS_RATIO
    counter_cx = cx + counter_radius * COUNTER_OFFSET_RATIO
    counter_bbox = [
        counter_cx - counter_radius, cy - counter_radius,
        counter_cx + counter_radius, cy + counter_radius,
    ]
    draw.ellipse(counter_bbox, fill=0)

    return mask.resize((size, size), Image.LANCZOS)


def render_icon(size):
    ground = build_gradient_image(size).convert("RGBA")
    mark_mask = build_mark_mask(size)
    mark_layer = Image.new("RGBA", (size, size), MARK_COLOR + (255,))
    out = ground.copy()
    out.paste(mark_layer, (0, 0), mark_mask)
    return out


def main():
    # (filename, size)
    sizes = [
        ("apple-touch-icon.png", 180),
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("favicon-32.png", 32),
    ]
    for name, s in sizes:
        render_icon(s).save(HERE / name)
        print(f"wrote {name} ({s}x{s})")

    preview = render_icon(512)
    preview_path = HERE.parent / "icon-preview.png"
    preview.save(preview_path)
    print(f"wrote {preview_path.name} (512x512, for eyeball review)")

    manifest = {
        "name": "Convivy Builder · Orient",
        # The home-screen label: an iPhone shows about 12 characters, so the
        # part name stands alone here (the operator, 2026-10-06), and "name" carries
        # the full lockup wherever there is room.
        "short_name": "Orient",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#FBF8F4",
        "theme_color": "#E8542F",
        "icons": [
            {"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    (HERE / "site.webmanifest").write_text(json.dumps(manifest, indent=2) + "\n")
    print("wrote site.webmanifest")


if __name__ == "__main__":
    main()
