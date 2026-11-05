"""Render only real NativeStatusLineTests PTY cell captures (requires Pillow).

Usage:
  python tests/render_statusline_evidence.py /tmp/paveway-bubble-evidence \
      --font /tmp/Paveway-DejaVuSansMNerdFontMono-Regular.ttf \
      --output docs/verification/windline-statusline

The font is an external evidence-generation input, not an application dependency.
No statusline renderer or synthetic state is imported by this helper.
"""

import argparse
import hashlib
import json
from pathlib import Path
import unicodedata

from PIL import Image, ImageDraw, ImageFont


ANSI = {
    "black": "#000000", "red": "#cd0000", "green": "#00cd00", "brown": "#cdcd00",
    "blue": "#0000ee", "magenta": "#cd00cd", "cyan": "#00cdcd", "white": "#e5e5e5",
    "brightblack": "#7f7f7f", "brightred": "#ff0000", "brightgreen": "#00ff00",
    "brightbrown": "#ffff00", "brightblue": "#5c5cff", "brightmagenta": "#ff00ff",
    "brightcyan": "#00ffff", "brightwhite": "#ffffff",
}


def color(value, default):
    if value == "default":
        return default
    if value in ANSI:
        return ANSI[value]
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value
    raise ValueError(f"Unsupported captured terminal color: {value!r}")


def render_capture(path, font, wide_font, size):
    data = json.loads(path.read_text())
    if data.get("source") != "isolated-native-pty" or not path.with_suffix(".ansi").is_file():
        raise ValueError(f"Not a real paired native PTY capture: {path}")
    columns, status = data["columns"], data["status_row"]
    if status is None:
        raise ValueError(f"No captured status row: {path}")
    ascent, descent = font.getmetrics()
    cell_width, cell_height = round(font.getlength("M")), ascent + descent
    start, stop = max(0, status - 9), min(data["rows"], status + 2)
    image = Image.new("RGB", (columns * cell_width, (stop - start) * cell_height), "#161d30")
    draw = ImageDraw.Draw(image)
    rows = data["cells"][start:stop]
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            foreground = color(cell["fg"], "#dae7f3")
            background = color(cell["bg"], "#161d30")
            if cell.get("reverse"):
                foreground, background = background, foreground
            draw.rectangle((x * cell_width, y * cell_height,
                            (x + 1) * cell_width - 1, (y + 1) * cell_height - 1), fill=background)
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            text = cell["data"]
            if not text or text.isspace():
                continue
            foreground = color(cell["bg"] if cell.get("reverse") else cell["fg"],
                               "#161d30" if cell.get("reverse") else "#dae7f3")
            glyph_font = wide_font if wide_font and unicodedata.east_asian_width(text[0]) in "WF" else font
            # Draw the captured glyph itself, including the real Nerd Font cap.
            # The image never draws replacement polygons, bubbles or meter cells.
            draw.text((x * cell_width, y * cell_height + ascent), text,
                      font=glyph_font, fill=foreground, anchor="ls",
                      stroke_width=0)
            if cell.get("underscore"):
                draw.line((x * cell_width, (y + 1) * cell_height - 2,
                           (x + 1) * cell_width - 1, (y + 1) * cell_height - 2), fill=foreground)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--wide-font", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/verification/windline-statusline"))
    parser.add_argument("--size", type=int, default=20)
    args = parser.parse_args()
    font = ImageFont.truetype(str(args.font), args.size)
    wide_font = ImageFont.truetype(str(args.wide_font), args.size) if args.wide_font else None
    args.output.mkdir(parents=True, exist_ok=True)
    paths = sorted(path for path in args.captures.glob("bubble-*.json")
                   if path.name != "bubble-rendering-manifest.json")
    if not paths:
        parser.error("No bubble-*.json PTY captures found")
    images = {}
    manifest = {"font": args.font.name, "font_sha256": hashlib.sha256(args.font.read_bytes()).hexdigest(),
                "size": args.size, "source": "isolated-native-pty", "captures": []}
    if args.wide_font:
        manifest.update({"wide_font": args.wide_font.name,
                         "wide_font_sha256": hashlib.sha256(args.wide_font.read_bytes()).hexdigest()})
    for path in paths:
        if path.stem.endswith("failure"):
            continue
        image = render_capture(path, font, wide_font, args.size)
        image.save(args.output / (path.stem + ".png"))
        images[path.stem] = image
        manifest["captures"].append({"name": path.name, "json_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                     "ansi_sha256": hashlib.sha256(path.with_suffix(".ansi").read_bytes()).hexdigest(),
                                     "image": path.stem + ".png", "pixels": list(image.size)})
    frames = [images[f"bubble-working-{index}"] for index in range(32)
              if f"bubble-working-{index}" in images]
    if frames:
        if len(frames) != 32:
            raise ValueError("Working evidence must contain all 32 actual PTY frames")
        frames[0].save(args.output / "bubble-working.gif", save_all=True, append_images=frames[1:],
                       duration=125, loop=0, optimize=False)
        selected = [frames[index] for index in (0, 6, 12, 18, 24, 30)]
        # Stack the captured status rows without redrawing their content.
        row_height = font.getmetrics()[0] + font.getmetrics()[1]
        strips = [image.crop((0, image.height - 2 * row_height, image.width, image.height))
                  for image in selected]
        sheet = Image.new("RGB", (strips[0].width, sum(image.height for image in strips)), "#161d30")
        top = 0
        for strip in strips:
            sheet.paste(strip, (0, top))
            top += strip.height
        sheet.save(args.output / "bubble-working-frames.png")
    # Raw captures remain in /tmp; the durable manifest contains hashes, not session content.
    provenance = json.dumps(manifest, indent=2) + "\n"
    (args.captures / "bubble-rendering-manifest.json").write_text(provenance)
    (args.output / "bubble-rendering-manifest.json").write_text(provenance)
    print(f"Rendered {len(images)} real PTY captures to {args.output}")


if __name__ == "__main__":
    main()
