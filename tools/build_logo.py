"""Regenerate every FleetHub logo file from one copy of the mark's geometry.

    python tools/build_logo.py

The mark is "hub and spokes" (option 1a of the FleetHub logo design): a hub in accent blue
linked to four managed machines, drawn on the console's 32px icon grid with round caps. It
replaced the thermometer, which dated from when this was a temperature monitor and said
nothing about a fleet.

Writes, all committed:

    hub/static/favicon.svg                      the app-icon tile, for the browser tab
    hub/static/apple-touch-icon.png             the same tile, 180px, for iOS home screens
    app/windows/runner/resources/app_icon.ico   the Flutter client's exe and tray icon
    agent/src/TempMonitorAgent/app.ico          the agent exe, and so the ticket tray icon
    agent-android/.../Resources/mipmap-*/       the Android agent's launcher icon, legacy PNGs
    agent-android/.../Resources/drawable/       its adaptive-icon layers and notification icon

**Android gets vectors where it can take them.** An adaptive icon (API 26+, which is this
app's floor) is a background layer and a foreground layer that the launcher masks into its own
shape, so the foreground is the bare mark in a 108dp canvas with the tile colour behind it --
not the rounded tile, which a circle mask would cut the corners off. The PNGs are only for
launchers that ignore adaptive icons, and the notification icon is a white silhouette because
the status bar draws every small icon as one: the near-black hub would simply vanish.

The console's own copies of the mark (sidebar, mobile topbar, login, invite, onboarding) are
**not** written here: they are inline SVG in `hub/templates/partials/_logo.html`, coloured by
`currentColor` and `var(--accent)` so they follow the light/dark theme, which an `<img>` cannot.
If the geometry below changes, change that partial in the same commit -- the two are the same
four numbers, and tests/test_logo.py fails until they agree again.

**No Pillow, no cairosvg, no browser.** The PNGs are rasterised here from signed distance
functions with analytic anti-aliasing, and written with zlib. The rejected alternatives were
each a dependency for one script that runs once a year: Pillow cannot draw an SVG anyway,
cairosvg drags in native Cairo on Windows, and exporting from a design tool by hand is how
the four sizes drift apart. The output is deterministic, so a re-run with no change is no diff.

ICO entries are 32-bit BMP below 256px and PNG at 256px. PNG-in-ICO at every size is smaller
but `rc.exe` and some shell paths still mis-handle it for the small sizes, and a tray icon that
silently falls back to `SystemIcons.Information` is the failure we would not notice.
"""
import math
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- geometry (32px grid)
HUB = (16, 16, 5.5)
NODES = [(7, 7), (25, 7), (7, 25), (25, 25)]
NODE_R = 3.5
SPOKE_W = 2.5

# The app-icon tile: a 64px square with 16px corners holding the mark at 38px, as the design
# draws it. In tile units the mark is scaled by 38/64 and centred.
TILE_RADIUS = 8            # of 32
MARK_SCALE = 38 / 64
MARK_OFFSET = (32 - 32 * MARK_SCALE) / 2

# Colours per surface, from the design. On the tile the roles invert: the spokes go white
# and the hub goes near-black, because blue-on-blue would vanish.
TILE = "#3b82f6"
TILE_PRIMARY = "#ffffff"
TILE_HUB = "#0b0e14"


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------- SVG
def tile_svg():
    s, o = MARK_SCALE, MARK_OFFSET
    spokes = "".join(f"M16 16L{x} {y}" for x, y in NODES)
    nodes = "".join(f'<circle cx="{x}" cy="{y}" r="{NODE_R}"/>' for x, y in NODES)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="{TILE_RADIUS}" fill="{TILE}"/>'
        f'<g transform="translate({o:g} {o:g}) scale({s:g})">'
        f'<path d="{spokes}" stroke="{TILE_PRIMARY}" stroke-width="{SPOKE_W}" '
        'stroke-linecap="round" fill="none"/>'
        f'<g fill="{TILE_PRIMARY}">{nodes}</g>'
        f'<circle cx="{HUB[0]}" cy="{HUB[1]}" r="{HUB[2]}" fill="{TILE_HUB}"/>'
        "</g></svg>\n"
    )


# ---------------------------------------------------------------- raster
def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def sd_segment(px, py, ax, ay, bx, by, half_width):
    # Distance to a round-capped segment -- exactly what stroke-linecap="round" draws.
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) - half_width


def sd_round_rect(px, py, size, r):
    qx = abs(px - size / 2) - (size / 2 - r)
    qy = abs(py - size / 2) - (size / 2 - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def render_tile(size):
    """RGBA bytes, top row first, of the tile at `size` pixels square."""
    unit = 32 / size                       # grid units per pixel
    tile, white, hub = hex_rgb(TILE), hex_rgb(TILE_PRIMARY), hex_rgb(TILE_HUB)
    s, o = MARK_SCALE, MARK_OFFSET
    out = bytearray(size * size * 4)

    def cov(d, px=unit):
        # Coverage from distance: half a pixel either side of the edge is the ramp.
        return max(0.0, min(1.0, 0.5 - d / px))

    for j in range(size):
        for i in range(size):
            x, y = (i + 0.5) * unit, (j + 0.5) * unit
            alpha = cov(sd_round_rect(x, y, 32, TILE_RADIUS))
            if alpha <= 0.0:
                continue
            mx, my, mp = (x - o) / s, (y - o) / s, unit / s     # into mark space
            d = min(sd_segment(mx, my, 16, 16, nx, ny, SPOKE_W / 2) for nx, ny in NODES)
            d = min(d, *(sd_circle(mx, my, nx, ny, NODE_R) for nx, ny in NODES))
            c_white = cov(d, mp)
            c_hub = cov(sd_circle(mx, my, *HUB), mp)
            rgb = [tile[k] * (1 - c_white) + white[k] * c_white for k in range(3)]
            rgb = [rgb[k] * (1 - c_hub) + hub[k] * c_hub for k in range(3)]
            p = (j * size + i) * 4
            out[p:p + 4] = bytes([round(rgb[0]), round(rgb[1]), round(rgb[2]), round(alpha * 255)])
    return bytes(out)


def render_round(size):
    """The tile clipped to a circle, for the legacy `ic_launcher_round` slot."""
    rgba = bytearray(render_tile(size))
    unit = 32 / size
    for j in range(size):
        for i in range(size):
            x, y = (i + 0.5) * unit, (j + 0.5) * unit
            alpha = max(0.0, min(1.0, 0.5 - sd_circle(x, y, 16, 16, 16) / unit))
            p = (j * size + i) * 4 + 3
            rgba[p] = round(rgba[p] * alpha)
    return bytes(rgba)


# ---------------------------------------------------------------- Android vectors
ANDROID_RES = "agent-android/src/FleetHubAgent.Android/Resources"

#: Launcher density buckets and their 48dp sizes in pixels.
ANDROID_DENSITIES = (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144),
                     ("xxxhdpi", 192))

#: The adaptive canvas is 108dp, of which the inner 66dp circle is all a launcher promises to
#: show. At 1.75 the mark is 56dp wide and its outermost node edge sits 28.4dp from centre,
#: inside the 33dp radius with room to spare on the most aggressive (squircle) masks.
ADAPTIVE_SCALE = 1.75
ADAPTIVE_OFFSET = (108 - 32 * ADAPTIVE_SCALE) / 2

ANDROID_NS = 'xmlns:android="http://schemas.android.com/apk/res/android"'


def _vector_mark(primary, hub, viewport, scale, offset):
    spokes = "".join(f"M16,16L{x},{y}" for x, y in NODES)
    nodes = "".join(
        f'<path android:fillColor="{primary}" android:pathData="{_circle_path(x, y, NODE_R)}"/>'
        for x, y in NODES)
    hub_path = (f'<path android:fillColor="{hub}" '
                f'android:pathData="{_circle_path(HUB[0], HUB[1], HUB[2])}"/>')
    return (
        f'<vector {ANDROID_NS} android:width="{viewport}dp" android:height="{viewport}dp" '
        f'android:viewportWidth="{viewport}" android:viewportHeight="{viewport}">'
        f'<group android:translateX="{offset:g}" android:translateY="{offset:g}" '
        f'android:scaleX="{scale:g}" android:scaleY="{scale:g}">'
        f'<path android:strokeColor="{primary}" android:strokeWidth="{SPOKE_W:g}" '
        f'android:strokeLineCap="round" android:pathData="{spokes}"/>'
        f"{nodes}{hub_path}"
        "</group></vector>\n"
    )


def _circle_path(cx, cy, r):
    # VectorDrawable has no <circle>; two arcs are the standard spelling of one.
    return (f"M{cx - r:g},{cy:g}a{r:g},{r:g} 0,1 1,{2 * r:g},0"
            f"a{r:g},{r:g} 0,1 1,{-2 * r:g},0z")


def android_foreground():
    return _vector_mark(TILE_PRIMARY, TILE_HUB, 108, ADAPTIVE_SCALE, ADAPTIVE_OFFSET)


def android_monochrome():
    # Themed icons (Android 13+) tint this one colour, so the hub is drawn in the same colour
    # as everything else: a two-tone mark has no meaning once the launcher recolours it.
    return _vector_mark("#ffffff", "#ffffff", 108, ADAPTIVE_SCALE, ADAPTIVE_OFFSET)


def android_background():
    return (f'<vector {ANDROID_NS} android:width="108dp" android:height="108dp" '
            'android:viewportWidth="108" android:viewportHeight="108">'
            f'<path android:fillColor="{TILE}" android:pathData="M0,0h108v108h-108z"/>'
            "</vector>\n")


def android_notification():
    # 24dp, the status-bar size, with the mark filling the 32-unit grid edge to edge minus
    # the node radius it already leaves. White on transparent; the system tints it.
    return _vector_mark("#ffffff", "#ffffff", 24, 24 / 32, 0)


def android_adaptive():
    return (f'<adaptive-icon {ANDROID_NS}>'
            '<background android:drawable="@drawable/ic_launcher_background"/>'
            '<foreground android:drawable="@drawable/ic_launcher_foreground"/>'
            '<monochrome android:drawable="@drawable/ic_launcher_monochrome"/>'
            "</adaptive-icon>\n")


def png(size, rgba):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + rgba[r * size * 4:(r + 1) * size * 4] for r in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


def bmp_entry(size, rgba):
    # A DIB as ICO wants it: BITMAPINFOHEADER with doubled height, BGRA rows bottom-up,
    # then a 1bpp AND mask. The mask is all zeros -- alpha already says what is transparent.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = bytearray()
    for r in range(size - 1, -1, -1):
        row = rgba[r * size * 4:(r + 1) * size * 4]
        for i in range(0, len(row), 4):
            pixels += bytes([row[i + 2], row[i + 1], row[i], row[i + 3]])
    mask_row = ((size + 31) // 32) * 4
    return header + bytes(pixels) + bytes(mask_row * size)


def ico(sizes):
    images = []
    for size in sizes:
        rgba = render_tile(size)
        images.append((size, png(size, rgba) if size >= 256 else bmp_entry(size, rgba)))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        dim = 0 if size >= 256 else size       # 0 means 256 in an ICONDIRENTRY
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def write(rel, data):
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Binary even for the SVG, so Windows never writes it with CRLF and a re-run there is
    # still byte-identical to one anywhere else.
    with open(path, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
    print(f"wrote {rel} ({len(data)} bytes)")


def main():
    icon_sizes = [16, 20, 24, 32, 40, 48, 64, 256]
    write("hub/static/favicon.svg", tile_svg())
    write("hub/static/apple-touch-icon.png", png(180, render_tile(180)))
    app_ico = ico(icon_sizes)
    write("app/windows/runner/resources/app_icon.ico", app_ico)
    write("agent/src/TempMonitorAgent/app.ico", app_ico)

    for bucket, size in ANDROID_DENSITIES:
        write(f"{ANDROID_RES}/mipmap-{bucket}/ic_launcher.png", png(size, render_tile(size)))
        write(f"{ANDROID_RES}/mipmap-{bucket}/ic_launcher_round.png",
              png(size, render_round(size)))
    write(f"{ANDROID_RES}/mipmap-anydpi-v26/ic_launcher.xml", android_adaptive())
    write(f"{ANDROID_RES}/mipmap-anydpi-v26/ic_launcher_round.xml", android_adaptive())
    write(f"{ANDROID_RES}/drawable/ic_launcher_foreground.xml", android_foreground())
    write(f"{ANDROID_RES}/drawable/ic_launcher_monochrome.xml", android_monochrome())
    write(f"{ANDROID_RES}/drawable/ic_launcher_background.xml", android_background())
    write(f"{ANDROID_RES}/drawable/ic_stat_fleethub.xml", android_notification())
    return 0


if __name__ == "__main__":
    sys.exit(main())
