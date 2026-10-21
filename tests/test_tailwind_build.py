"""The Tailwind pipeline's shape: pinned, preflight-free, committed, linked.

The silent failures this file exists to catch:

  * **A deployed hub serving no utilities at all.** Hub self-update ships hub/ as plain files
    and never builds anything, so an app.css that was never committed -- or a base.html that
    stopped linking it -- means every Tailwind class in the console has no CSS, and nothing
    errors anywhere.
  * **Preflight sneaking in.** Tailwind's reset restyles every heading, button and border in
    the console at once. components.css still styles most pages, so preflight arriving in a
    routine rebuild would visibly break pages nobody touched.
  * **An unpinned or unverified CLI.** The build script downloads an executable. A version
    without a digest beside it is a binary trusted on first sight.
  * **The binary getting committed.** 110 MB in git history, on every clone, forever.
  * **An unlayered reset outranking every utility.** Tailwind writes its utilities into
    `@layer utilities`, and an unlayered rule beats any layered one whatever its specificity.
    base.css's `* { margin: 0; padding: 0 }` was unlayered, so from 1.114.0 to 1.162.0 every
    margin and padding utility in the console was dead -- the Ctrl+K palette sat pinned to
    the top-left corner with its rows against its border, and nothing errored.
  * **A class with no CSS behind it**, because app.css was not rebuilt after it was written.

What this cannot check in full is whether app.css is up to date -- that needs the CLI, which
the test environment does not have. It checks the classes most likely to be missing and least
likely to be anything but Tailwind: arbitrary values and variants (`[`, `:` or `/` in the
name). A plain `flex` or `hidden` is also a components.css or JS-hook name, so it cannot be
told apart from one here. Rebuild after changing classes.

Run from the repo root.
"""
import os
import re
import sys

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "hub", "static", "css")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_output_is_committed_and_linked():
    print("\n-- app.css exists and every page loads it --")
    path = os.path.join(CSS, "app.css")
    check("hub/static/css/app.css exists", os.path.isfile(path))
    check("...and is not empty", os.path.isfile(path) and os.path.getsize(path) > 0)
    base = read("hub", "templates", "base.html")
    check("base.html links it", "filename='css/app.css'" in base)
    check("...after components.css, so a utility wins over the rule it adjusts",
          base.find("css/components.css") < base.find("css/app.css"))


def test_no_preflight():
    print("\n-- no preflight while components.css still styles the console --")
    source = read("hub", "static", "css", "tailwind.input.css")
    directives = "\n".join(line for line in source.splitlines()
                           if line.strip().startswith("@"))
    check("the input imports theme and utilities only",
          'tailwindcss/theme.css' in directives and 'tailwindcss/utilities.css' in directives)
    check("...and not the whole framework", not re.search(r'@import\s+"tailwindcss"\s*;', directives))
    check("...nor preflight by name", "preflight" not in directives)
    path = os.path.join(CSS, "app.css")
    if os.path.isfile(path):
        built = read("hub", "static", "css", "app.css")
        # Preflight's signature rule: the universal box-sizing/border reset.
        check("the built file carries no preflight reset",
              not re.search(r"\*,\s*:after,\s*:before|\*,\s*::after,\s*::before", built))


def test_colours_come_from_the_tokens():
    print("\n-- theme colours are tokens.css variables, not a second palette --")
    source = read("hub", "static", "css", "tailwind.input.css")
    check("the theme block is inline, so var() resolves at runtime", "@theme inline" in source)
    tokens = read("hub", "static", "css", "tokens.css")
    for name in re.findall(r"--color-[a-z-]+:\s*var\((--[a-z-]+)\)", source):
        check(f"{name} is defined in tokens.css", f"{name}:" in tokens)


def test_the_cli_is_pinned_and_verified():
    print("\n-- the build script pins a version and verifies its digest --")
    script = read("tools", "build_css.ps1")
    version = re.search(r"\$TailwindVersion\s*=\s*'(v\d+\.\d+\.\d+)'", script)
    digest = re.search(r"\$TailwindSha256\s*=\s*'([0-9a-f]{64})'", script)
    check("an exact version is pinned", version is not None)
    check("...with a SHA-256 beside it", digest is not None)
    check("the digest is checked before the binary is kept", "Test-Digest $partial" in script)
    check("...and again before an existing one is run", "Test-Digest $exe" in script)
    check("the script does not assign PowerShell's automatic $input",
          not re.search(r"^\s*\$input\s*=", script, re.M))
    check("the downloaded binary is gitignored", "tools/.bin/" in read(".gitignore"))


def test_the_reset_is_below_the_utilities():
    print("\n-- base.css's reset is layered, so utilities can override it --")
    base_css = read("hub", "static", "css", "base.css")
    # Every rule outside a layer block, at the top level: the universal reset must not be one.
    unlayered = re.sub(r"@layer\s+reset\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", base_css)
    unlayered = re.sub(r"/\*.*?\*/", "", unlayered, flags=re.S)
    check("base.css wraps its reset in @layer reset", re.search(r"@layer\s+reset\s*\{", base_css) is not None)
    check("...and no unlayered `* {` rule is left", not re.search(r"(^|\})\s*\*\s*\{", unlayered))
    page = read("hub", "templates", "base.html")
    # Layer order is order of first appearance: base.css must name `reset` before app.css
    # names theme and utilities, or the reset becomes the HIGHEST layer.
    check("base.css is linked before app.css, so `reset` is the lowest layer",
          0 <= page.find("css/base.css") < page.find("css/app.css"))


def css_escape(name):
    """The selector Tailwind writes for a class: everything outside [A-Za-z0-9_-] escaped."""
    return "".join(c if c.isalnum() or c in "-_" else "\\" + c for c in name)


def tailwind_only(token):
    """An arbitrary-value or variant class: a name only Tailwind could have produced."""
    if not token or "{" in token or "}" in token or "://" in token:
        return False
    return bool(re.search(r"[\[:/]", token)) and re.match(r"^[a-z!-]", token) is not None


TEMPLATE_CLASS = re.compile(r'\bclass="([^"]*)"')
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")
# The places a JS file names classes: className = '...', classList.add/remove/toggle('...'),
# and common.js's el('tag', '...').
JS_CLASS = re.compile(
    r"(?:className\s*=\s*|classList\.(?:add|remove|toggle)\(\s*|\bel\(\s*'[a-z0-9]+'\s*,\s*)"
    r"(['\"])([^'\"]*)\1")


def used_classes():
    """(where, class) for every Tailwind-only class in a template or a JS class literal."""
    found = set()
    for folder, _, files in os.walk(os.path.join(ROOT, "hub", "templates")):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for attr in TEMPLATE_CLASS.findall(text):
                for token in JINJA.sub(" ", attr).split():
                    if tailwind_only(token):
                        found.add((os.path.relpath(path, ROOT), token))
    js = os.path.join(ROOT, "hub", "static", "js")
    for name in os.listdir(js):
        if not name.endswith(".js"):
            continue
        path = os.path.join(js, name)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for _, value in JS_CLASS.findall(text):
            for token in value.split():
                if tailwind_only(token):
                    found.add((os.path.relpath(path, ROOT), token))
    return sorted(found)


def test_built_file_has_every_arbitrary_class():
    print("\n-- every arbitrary-value / variant class in use has CSS in app.css --")
    path = os.path.join(CSS, "app.css")
    if not os.path.isfile(path):
        check("app.css exists to check against", False)
        return
    built = read("hub", "static", "css", "app.css")
    used = used_classes()
    check("the scan finds the palette's classes (the scan itself works)",
          any(token == "mt-[12vh]" for _, token in used))
    missing = [(where, token) for where, token in used if "." + css_escape(token) not in built]
    for where, token in missing:
        print(f"       missing: {token}  ({where})")
    check(f"all {len(used)} are in app.css -- else run tools/build_css.ps1", not missing)


def main():
    test_the_output_is_committed_and_linked()
    test_no_preflight()
    test_colours_come_from_the_tokens()
    test_the_cli_is_pinned_and_verified()
    test_the_reset_is_below_the_utilities()
    test_built_file_has_every_arbitrary_class()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
