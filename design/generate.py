#!/usr/bin/env python3
"""Generate the platform token files from tokens.json.

WHY GENERATED RATHER THAN COPIED. Three surfaces render this system and
"keep them consistent" by discipline has never worked in any codebase. A
colour that differs between iOS and the hub is now impossible to introduce
by hand - it would have to be introduced in tokens.json, once, for all three.

Each output carries a DO NOT EDIT header naming this script, so the next
person to reach for the generated file is told where to go instead.
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
TOKENS = json.loads((HERE / "tokens.json").read_text())
BANNER = "GENERATED FROM design/tokens.json - DO NOT EDIT.\nRun design/generate.py after changing a token."


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def swift() -> str:
    out = [f"// {l}" for l in BANNER.split("\n")]
    out += ["", "import SwiftUI", "", "/// The zippie visual language, shared with the hub and the Android app.",
            "enum Tok {", "    enum Color_ {}", "}", ""]
    out += ["extension Ink {"]
    for name, v in TOKENS["color"].items():
        lr, lg, lb = hex_to_rgb(v["light"])
        dr, dg, db = hex_to_rgb(v["dark"])
        out += [f"    /// {v['use']}",
                f"    static let gen_{name} = Color(",
                f"        light: .init(red: {lr:.4f}, green: {lg:.4f}, blue: {lb:.4f}),",
                f"        dark: .init(red: {dr:.4f}, green: {dg:.4f}, blue: {db:.4f}))"]
    out += ["}", "", "enum StateWord {"]
    for k, v in TOKENS["state"].items():
        if k.startswith("$"):
            continue
        out.append(f'    static let {k} = "{v}"')
    out += ["}", ""]
    return "\n".join(out)


def kotlin() -> str:
    out = [f"// {l}" for l in BANNER.split("\n")]
    out += ["", "package app.zippie.companion.design", "",
            "import androidx.compose.ui.graphics.Color", "",
            "/** The zippie visual language, shared with the hub and the iOS app. */",
            "object Tok {"]
    for name, v in TOKENS["color"].items():
        out += [f"    /** {v['use']} */",
                f'    val {name}Light = Color(0xFF{v["light"].lstrip("#")})',
                f'    val {name}Dark = Color(0xFF{v["dark"].lstrip("#")})']
    for name, v in TOKENS["space"].items():
        if name.startswith("$"):
            continue
        out.append(f"    const val space_{name} = {v}")
    out.append("")
    out.append("    object StateWord {")
    for k, v in TOKENS["state"].items():
        if k.startswith("$"):
            continue
        out.append(f'        const val {k} = "{v}"')
    out += ["    }", "}", ""]
    return "\n".join(out)


def css() -> str:
    out = ["/*", *[f"  {l}" for l in BANNER.split("\n")], "*/", "", ":root {"]
    for name, v in TOKENS["color"].items():
        out.append(f"  /* {v['use']} */")
        out.append(f"  --c-{name}: {v['light']};")
    for name, v in TOKENS["space"].items():
        if name.startswith("$"):
            continue
        out.append(f"  --s-{name}: {v}px;")
    for name, v in TOKENS["type"].items():
        out.append(f"  --t-{name}-size: {v['size']}px;")
    out += ["}", "", "@media (prefers-color-scheme: dark) {", "  :root {"]
    for name, v in TOKENS["color"].items():
        out.append(f"    --c-{name}: {v['dark']};")
    out += ["  }", "}", ""]
    return "\n".join(out)


TARGETS = {
    "../companion/ZippieCompanionApp/Design/Tokens.generated.swift": swift,
    "../companion-android/app/src/main/java/app/zippie/companion/design/Tokens.generated.kt": kotlin,
    "../hub/static/tokens.generated.css": css,
}

if __name__ == "__main__":
    check = "--check" in sys.argv
    drift = []
    for rel, fn in TARGETS.items():
        p = (HERE / rel).resolve()
        body = fn()
        if check:
            if not p.is_file() or p.read_text() != body:
                drift.append(str(p))
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        print(f"wrote {p.relative_to(HERE.parent)}")
    if check and drift:
        print("DRIFT - regenerate with design/generate.py:")
        for d in drift:
            print("  " + d)
        sys.exit(1)
    if check:
        print("tokens in sync")
