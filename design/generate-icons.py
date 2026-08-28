#!/usr/bin/env python3
"""Generate every app icon from design/zippie-mark.png.

WHY GENERATED, for the same reason design/generate.py gives for the tokens:
there are 21 icon files across two platforms and five densities, and keeping
them consistent by hand has never worked in any codebase. Re-cropping one of
them by eye is how an icon ends up subtly different on one density.

    design/zippie-mark.png          the master. TRANSPARENT background, and it
                                    matters - see below.
    companion/.../AppIcon.appiconset/icon_1024.png
    companion-android/.../mipmap-*/ic_launcher{,_round,_foreground,_background}.png

THE MASTER IS TRANSPARENT, NOT BLACK. Measured: 266229 of 1572516 pixels carry
any alpha at all, which is the circles and nothing else. What looks like a black
background in an image viewer is the viewer. That is exactly what Android's
adaptive FOREGROUND layer needs, and exactly what iOS forbids - the App Store
rejects an icon with an alpha channel outright - so the iOS path flattens onto
GROUND and the Android foreground does not.

THE ADAPTIVE SAFE ZONE IS THE ONE REAL JUDGEMENT HERE. An adaptive icon is
108dp, of which the outer 18dp on every side is bleed the launcher may crop for
masking and parallax. That leaves a 72dp viewport, and a CIRCULAR mask is a 72dp
circle - so content only never clips if its bounding box fits inside that
circle.

THE MARK IS NEARLY SQUARE, AND THAT IS WHY THIS WORKS NOW. The first mark was
904x760 - wide and flat - whose bounding box corners sat outside any circle that
left the mark a usable size, and on a real launcher it read exactly as badly as
that implies: "it doesn't really fit in the circle". The current mark crops to
1214x1201, essentially square, so its diagonal is far closer to its width and a
generous span still clears the mask.

MARK_SPAN was chosen by rendering candidates under the circle and looking at
them, not by arithmetic alone - `--preview` regenerates that sheet. 0.62 clears
the mask with visible breathing room; 0.68 fits too but the corner circles come
close enough to the edge to read as tight, which is the complaint this replaces.
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent
MASTER = HERE / "zippie-mark.png"

# The flattened background. WHITE, changed 2026-08-19 after seeing the black
# one on a real launcher: it read as a dark tile among light ones and the
# operator's word for it was "you only set it to dark mode, that was weird".
#
# White is also what the mark was drawn for - the master is transparent, and
# every reference rendering of it has been on white - and it matches the muster
# console's light ground, which is the other surface this estate shows a mark on.
# Change it here and every flattened surface follows.
GROUND = (255, 255, 255, 255)

# Fraction of the adaptive canvas the mark spans horizontally. Chosen by LOOKING
# at --preview. 0.667 is the
# 72dp square (clips the end circles on a round mask); 0.51 fits the 72dp
# circle exactly (safe, small). See the module docstring.
MARK_SPAN = 0.62

# iOS is full-bleed with the system rounding the corners, so the mark can be
# larger than on Android - there is no mask that can crop it to a circle.
IOS_SPAN = 0.72

# iOS 18 app-icon appearances. Written by this script rather than by hand so
# the three filenames and the catalogue cannot drift apart - a missing variant
# is not an error, it is silently no dark icon.
IOS_CONTENTS = """{
  "images" : [
    {
      "filename" : "icon_1024.png",
      "idiom" : "universal",
      "platform" : "ios",
      "size" : "1024x1024"
    },
    {
      "appearances" : [
        {
          "appearance" : "luminosity",
          "value" : "dark"
        }
      ],
      "filename" : "icon_1024_dark.png",
      "idiom" : "universal",
      "platform" : "ios",
      "size" : "1024x1024"
    },
    {
      "appearances" : [
        {
          "appearance" : "luminosity",
          "value" : "tinted"
        }
      ],
      "filename" : "icon_1024_tinted.png",
      "idiom" : "universal",
      "platform" : "ios",
      "size" : "1024x1024"
    }
  ],
  "info" : { "author" : "xcode", "version" : 1 }
}
"""

# Android density buckets: (dir, adaptive layer px, pre-masked legacy px).
# The adaptive layers are 108dp, the legacy bitmaps 48dp, at each scale.
DENSITIES = [
    ("mdpi", 108, 48),
    ("hdpi", 162, 72),
    ("xhdpi", 216, 96),
    ("xxhdpi", 324, 144),
    ("xxxhdpi", 432, 192),
]


def load_mark() -> Image.Image:
    """The master, cropped to its ink so every placement below is predictable.

    Without the crop, the master's own margins become part of the scale, and a
    future re-export with different padding would silently resize every icon.
    """
    mark = Image.open(MASTER).convert("RGBA")
    box = mark.getbbox()          # alpha-aware: the circles, not the canvas
    if box is None:
        raise SystemExit(f"{MASTER} has no opaque pixels - wrong file?")
    return mark.crop(box)


def place(mark: Image.Image, canvas: int, span: float,
          ground: tuple | None) -> Image.Image:
    """The mark centred on a square canvas, scaled so its WIDTH is `span`.

    Width rather than the longer edge: this mark is wider than tall, so width
    is what binds, and driving off the longer edge would make the constant mean
    something different if the art ever changed shape.
    """
    out = Image.new("RGBA", (canvas, canvas), ground or (0, 0, 0, 0))
    target_w = max(1, round(canvas * span))
    target_h = max(1, round(target_w * mark.height / mark.width))
    scaled = mark.resize((target_w, target_h), Image.LANCZOS)
    out.alpha_composite(scaled, ((canvas - target_w) // 2,
                                 (canvas - target_h) // 2))
    return out


def circle_masked(image: Image.Image) -> Image.Image:
    """Pre-masked round bitmap for launchers older than API 26, which ignore
    the adaptive XML entirely and use this file as-is."""
    size = image.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = image.copy()
    out.putalpha(mask)
    return out


def flatten(image: Image.Image) -> Image.Image:
    """RGB, no alpha channel at all. The App Store rejects an icon that has
    one, and it rejects it after upload rather than at build time."""
    ground = Image.new("RGB", image.size, GROUND[:3])
    ground.paste(image, mask=image.split()[3])
    return ground


def silhouette(image: Image.Image, colour: tuple) -> Image.Image:
    """One flat colour in the shape of the mark, transparency preserved.

    This is what a THEMED icon is: the launcher (Android 13+) and the system
    (iOS tinted) throw the colour away and recolour the alpha to match the
    wallpaper or the tint. Supplying the gradient here would not survive, and
    an icon that ships no monochrome layer simply does not participate - it
    keeps its full-colour self on a themed home screen, which is the thing
    that looks out of place.
    """
    flat = Image.new("RGBA", image.size, colour)
    flat.putalpha(image.split()[3])
    return flat


def grayscale(image: Image.Image) -> Image.Image:
    """Luminance, alpha kept. iOS maps a grey ramp onto the user's tint, so a
    flat silhouette would come back as one flat colour and lose the mark's
    depth - unlike Android, which recolours wholesale."""
    grey = image.convert("LA").convert("RGBA")
    grey.putalpha(image.split()[3])
    return grey


def write(path: pathlib.Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    print(f"  {path.relative_to(REPO)}  {image.size[0]}x{image.size[1]}")


def preview(mark: Image.Image, out: pathlib.Path) -> None:
    """Two rows: the masks a launcher may apply, and the themed variants.

    The point is to make MARK_SPAN and the theming decisions things somebody
    LOOKS at. A number in a diff cannot show that the end circles of the Z are
    being clipped, and "supports dark mode" cannot be read off a Contents.json.
    """
    cell, pad = 216, 24
    cols, rows = 4, 2
    sheet = Image.new("RGBA",
                      (cell * cols + pad * (cols + 1), (cell + pad) * rows + pad),
                      (28, 28, 32, 255))

    def at(col, row):
        return (pad + col * (cell + pad), pad + row * (cell + pad))

    # Row 1 - the three launcher masks, full colour.
    layer = place(mark, cell, MARK_SPAN, GROUND)
    square = Image.new("L", (cell, cell), 255)
    circle = Image.new("L", (cell, cell), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, cell - 1, cell - 1), fill=255)
    squircle = Image.new("L", (cell, cell), 0)
    ImageDraw.Draw(squircle).rounded_rectangle(
        (0, 0, cell - 1, cell - 1), radius=cell // 4, fill=255)
    for i, mask in enumerate((square, circle, squircle)):
        tile = layer.copy()
        tile.putalpha(mask)
        sheet.alpha_composite(tile, at(i, 0))

    # iOS dark: transparent art over the dark ground the SYSTEM draws. Shown on
    # that ground rather than on nothing, because "it has alpha" is not the
    # question - how it reads once composited is.
    ios_dark = Image.new("RGBA", (cell, cell), (22, 22, 24, 255))
    ios_dark.alpha_composite(place(mark, cell, IOS_SPAN, None))
    ios_dark.putalpha(squircle)
    sheet.alpha_composite(ios_dark, at(3, 0))

    # Row 2 - themed. Android recolours the monochrome layer wholesale; iOS
    # maps a grey ramp onto the tint, which is why one is flat and one is not.
    mono = silhouette(place(mark, cell, MARK_SPAN, None), (255, 255, 255, 255))
    for i, (bg, ink) in enumerate((((236, 236, 238, 255), (60, 60, 66, 255)),
                                   ((32, 32, 36, 255), (226, 226, 232, 255)))):
        tile = Image.new("RGBA", (cell, cell), bg)
        tinted = Image.new("RGBA", (cell, cell), ink)
        tinted.putalpha(mono.split()[3])
        tile.alpha_composite(tinted)
        tile.putalpha(squircle)
        sheet.alpha_composite(tile, at(i, 1))

    # iOS tinted: the grey ramp under a representative system tint.
    grey = grayscale(place(mark, cell, IOS_SPAN, None))
    tint = Image.new("RGBA", (cell, cell), (24, 24, 28, 255))
    ramp = grey.convert("L").point(lambda v: v)
    coloured = Image.merge("RGBA", (
        ramp.point(lambda v: int(v * 0.45)),
        ramp.point(lambda v: int(v * 0.75)),
        ramp.point(lambda v: min(255, int(v * 1.0))),
        grey.split()[3]))
    tint.alpha_composite(coloured)
    tint.putalpha(squircle)
    sheet.alpha_composite(tint, at(2, 1))

    # And the legacy pre-masked round bitmap, which older launchers use as-is.
    legacy = circle_masked(place(mark, cell, MARK_SPAN, GROUND))
    sheet.alpha_composite(legacy, at(3, 1))

    write(out, sheet)


def main() -> None:
    mark = load_mark()
    print(f"mark: {mark.width}x{mark.height} (cropped to ink)")

    if "--preview" in sys.argv:
        preview(mark, HERE / "icon-preview.png")
        return

    print("iOS:")
    appicon = REPO / "companion/ZippieCompanionApp/Assets.xcassets/AppIcon.appiconset"
    ios_mark = place(mark, 1024, IOS_SPAN, None)
    # Light/default MUST be opaque - the App Store rejects an alpha channel.
    write(appicon / "icon_1024.png", flatten(ios_mark))
    # Dark and tinted keep their transparency ON PURPOSE: from iOS 18 the
    # system draws its own background behind these two, and baking one in
    # produces a visible square sitting on top of the system's.
    write(appicon / "icon_1024_dark.png", ios_mark)
    write(appicon / "icon_1024_tinted.png", grayscale(ios_mark))
    (appicon / "Contents.json").write_text(IOS_CONTENTS)
    print(f"  {(appicon / 'Contents.json').relative_to(REPO)}  3 appearances")

    print("Android:")
    res = REPO / "companion-android/app/src/main/res"
    for name, adaptive, legacy in DENSITIES:
        d = res / f"mipmap-{name}"
        # Foreground keeps its transparency; the launcher composites it over
        # the background layer and applies the mask to the pair.
        write(d / "ic_launcher_foreground.png", place(mark, adaptive, MARK_SPAN, None))
        write(d / "ic_launcher_background.png",
              Image.new("RGBA", (adaptive, adaptive), GROUND))
        # Themed icons, Android 13+. Same geometry as the foreground so the
        # mark does not appear to move when the user turns themed icons on.
        write(d / "ic_launcher_monochrome.png",
              silhouette(place(mark, adaptive, MARK_SPAN, None), (255, 255, 255, 255)))
        legacy_icon = place(mark, legacy, MARK_SPAN, GROUND)
        write(d / "ic_launcher.png", legacy_icon)
        write(d / "ic_launcher_round.png", circle_masked(legacy_icon))


if __name__ == "__main__":
    main()
