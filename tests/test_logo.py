"""Catches the FleetHub mark drifting between its two copies.

The console draws the logo inline from hub/templates/partials/_logo.html (so it can follow
the theme), while the favicon, touch icon and .ico files come from tools/build_logo.py.
Both hold the same geometry and nothing else compares them: edit one and the sidebar and the
browser tab quietly show two different logos, with every page still rendering fine.

Also pins that the committed favicon.svg is what the generator produces today, i.e. that
nobody hand-edited the output or changed the geometry without re-running the script -- and
the same for the Android agent's vector layers, where a stale file is a phone launcher still
showing the old mark (or no mark: a missing layer is a blank adaptive icon, not a build error).
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_logo

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_committed_favicon_matches_generator():
    # Normalised for line endings only: a Windows checkout may hold CRLF.
    committed = read("hub/static/favicon.svg").replace("\r\n", "\n")
    check("hub/static/favicon.svg is what tools/build_logo.py writes",
          committed == build_logo.tile_svg())


def test_partial_matches_generator_geometry():
    partial = read("hub/templates/partials/_logo.html")
    spokes = "".join(f"M16 16L{x} {y}" for x, y in build_logo.NODES)
    check("partial draws the same spokes", f'd="{spokes}"' in partial)
    check("partial uses the same spoke width",
          f'stroke-width="{build_logo.SPOKE_W:g}"' in partial)
    circles = {(float(cx), float(cy), float(r)) for cx, cy, r in
               re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', partial)}
    expected = {(float(x), float(y), build_logo.NODE_R) for x, y in build_logo.NODES}
    expected.add(tuple(float(v) for v in build_logo.HUB))
    check("partial has the same four nodes and hub", circles == expected)


def test_android_vectors_match_generator():
    res = build_logo.ANDROID_RES
    for rel, expected in (
            ("drawable/ic_launcher_foreground.xml", build_logo.android_foreground()),
            ("drawable/ic_launcher_monochrome.xml", build_logo.android_monochrome()),
            ("drawable/ic_launcher_background.xml", build_logo.android_background()),
            ("drawable/ic_stat_fleethub.xml", build_logo.android_notification()),
            ("mipmap-anydpi-v26/ic_launcher.xml", build_logo.android_adaptive()),
            ("mipmap-anydpi-v26/ic_launcher_round.xml", build_logo.android_adaptive())):
        committed = read(f"{res}/{rel}").replace("\r\n", "\n")
        check(f"{rel} is what tools/build_logo.py writes", committed == expected)
    for bucket, _ in build_logo.ANDROID_DENSITIES:
        for name in ("ic_launcher.png", "ic_launcher_round.png"):
            check(f"mipmap-{bucket}/{name} exists",
                  os.path.isfile(os.path.join(ROOT, res, f"mipmap-{bucket}", name)))


def main():
    print("logo:")
    test_committed_favicon_matches_generator()
    test_android_vectors_match_generator()
    test_partial_matches_generator_geometry()
    print(f"==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
