"""Draw Rove's app icon: the six-ring mark in mint on deep-blue ribs, 1024 by 1024.

The mark is six open rings of equal radius, their centres 1.36 radii from the middle
at 60-degree steps, each ring crossing only its two neighbours. Each ring is cut once:
the cut starts at the ring's outer crossing with the next ring, so a single flat end
shows per ring. These are the numbers the banner in docs/assets was drawn with.

The page is rendered by Patchright's headless Chromium, the browser that
`patchright install chromium` provides for the tests. Run from the checkout:

    .venv/bin/python scripts/app_icon.py

It writes src/rove/assets/rove-app-icon.png (shipped in the wheel and applied to the
recruiting browser's app bundle) and the copy in docs/assets.
"""

import math
from pathlib import Path

from patchright.sync_api import sync_playwright

SIZE = 1024
MINT = "#CBF4E6"
# The blue ribs: top to bottom, with the rib highlight and shadow tints.
BLUE = [
    (0.0, "#8BB3ED"),
    (0.16, "#5685D4"),
    (0.33, "#2B58B1"),
    (0.5, "#193986"),
    (0.67, "#12265C"),
    (0.84, "#0B1530"),
    (1.0, "#040814"),
]
HIGHLIGHT, SHADOW = "#D6E4FF", "#03060F"
# Rib widths repeat this pattern, in pixels at scale 1.
PATTERN = [76, 102, 61, 76, 76, 61, 102, 76, 61, 76, 76, 102, 76, 76, 61, 76, 102, 61, 76, 76]
RINGS = 6
OFFSET = 1.36  # ring centres, in ring radii
STROKE = 0.28  # stroke width, in ring radii
GAP = 66.0  # degrees of each ring left open


def outer_crossing(rings: int, offset: float) -> float:
    """Angle on a ring, from its outward radial, of its outer crossing with the next ring."""
    chord = 2 * offset * math.sin(math.pi / rings)
    return (180.0 / rings + 90.0) - math.degrees(math.acos(chord / 2))


def ring_paths(radius: float, cx: float, cy: float, color: str) -> str:
    """The six rings as SVG arcs, scaled so their outer extent equals `radius`."""
    scale = radius / (1 + OFFSET + STROKE / 2)
    cut = outer_crossing(RINGS, OFFSET) - GAP / 2
    paths = []
    for i in range(RINGS):
        angle = -90.0 + i * 360.0 / RINGS
        ox = cx + scale * OFFSET * math.cos(math.radians(angle))
        oy = cy + scale * OFFSET * math.sin(math.radians(angle))
        start = math.radians(angle + cut + GAP / 2)
        end = math.radians(angle + cut + 360 - GAP / 2)
        paths.append(
            f'<path d="M{ox + scale * math.cos(start):.2f} {oy + scale * math.sin(start):.2f}'
            f"A{scale:.2f} {scale:.2f} 0 1 1 "
            f'{ox + scale * math.cos(end):.2f} {oy + scale * math.sin(end):.2f}" '
            f'fill="none" stroke="{color}" stroke-width="{scale * STROKE:.2f}" '
            'stroke-linecap="butt"/>'
        )
    return "".join(paths)


def squircle(cx: float, cy: float, half: float, exponent: float = 5.0, steps: int = 360) -> str:
    """The macOS icon shape: a superellipse, as a closed SVG path."""
    points = []
    for step in range(steps):
        t = 2 * math.pi * step / steps
        c, s = math.cos(t), math.sin(t)
        x = cx + half * math.copysign(abs(c) ** (2 / exponent), c)
        y = cy + half * math.copysign(abs(s) ** (2 / exponent), s)
        points.append(f"{x:.2f} {y:.2f}")
    return "M" + "L".join(points) + "Z"


def ribs(width: float, scale: float, start: float) -> list[tuple[float, float]]:
    out, x, i = [], start, 0
    while x < width:
        w = round(PATTERN[i % len(PATTERN)] * scale)
        out.append((x, w))
        x += w
        i += 1
    return out


def icon_svg() -> str:
    inset = SIZE * 100 / 1024  # Apple's icon grid: the shape fills 824 of 1024
    side = SIZE - 2 * inset
    shape = squircle(SIZE / 2, SIZE / 2, side / 2)
    vertical = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in BLUE)
    rib = (
        f'<stop offset="0" stop-color="{HIGHLIGHT}" stop-opacity="0.14"/>'
        f'<stop offset="0.04" stop-color="{HIGHLIGHT}" stop-opacity="0.14"/>'
        f'<stop offset="0.045" stop-color="{HIGHLIGHT}" stop-opacity="0.07"/>'
        f'<stop offset="0.5" stop-color="{HIGHLIGHT}" stop-opacity="0"/>'
        f'<stop offset="0.5" stop-color="{SHADOW}" stop-opacity="0"/>'
        f'<stop offset="0.85" stop-color="{SHADOW}" stop-opacity="0.12"/>'
        f'<stop offset="1" stop-color="{SHADOW}" stop-opacity="0.27"/>'
    )
    bars = "".join(
        f'<rect x="{x}" y="{inset}" width="{w}" height="{side}" fill="url(#rib)"/>'
        for x, w in ribs(SIZE, 1.15, inset - 30)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
        f'viewBox="0 0 {SIZE} {SIZE}">'
        "<defs>"
        f'<linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">{vertical}</linearGradient>'
        f'<linearGradient id="rib" x1="0" y1="0" x2="1" y2="0">{rib}</linearGradient>'
        f'<clipPath id="shape"><path d="{shape}"/></clipPath>'
        "</defs>"
        '<g clip-path="url(#shape)">'
        f'<rect x="0" y="0" width="{SIZE}" height="{SIZE}" fill="url(#ground)"/>'
        f"{bars}"
        "</g>"
        f"{ring_paths(side * 0.33, SIZE / 2, SIZE / 2, MINT)}"
        "</svg>"
    )


def render(svg: str, target: Path) -> None:
    html = (
        "<!doctype html><html><head><style>html,body{margin:0;background:transparent}"
        "svg{display:block}</style></head><body>" + svg + "</body></html>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": SIZE, "height": SIZE}, device_scale_factor=1)
            page.set_content(html)
            page.screenshot(path=str(target), omit_background=True, full_page=False)
        finally:
            browser.close()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    svg = icon_svg()
    package_copy = root / "src/rove/assets/rove-app-icon.png"
    package_copy.parent.mkdir(parents=True, exist_ok=True)
    render(svg, package_copy)
    docs_copy = root / "docs/assets/rove-app-icon.png"
    docs_copy.write_bytes(package_copy.read_bytes())
    print(package_copy)
    print(docs_copy)


if __name__ == "__main__":
    main()
