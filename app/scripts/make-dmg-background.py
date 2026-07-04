#!/usr/bin/env python3
"""Generate the DMG background (app/assets/dmg-background.png).

Dark canvas matching the app's palette, with a drag arrow between where
dmg-settings.py places the app icon (x=160) and the Applications alias (x=480),
plus an install hint. Run once and commit the PNG; re-run only to restyle.
"""
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 640, 400
BG = (28, 30, 33)          # --pc-bg dark
TXT = (241, 242, 244)      # --pc-text
SUB = (154, 160, 170)      # secondary
BRAND = (31, 158, 134)     # app-icon teal/green

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def font(size, bold=False):
    for name in (["/System/Library/Fonts/SFNS.ttf"] if not bold else
                 ["/System/Library/Fonts/SFNSDisplay-Bold.otf", "/System/Library/Fonts/SFNS.ttf"]):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def center_text(y, text, f, fill):
    w = d.textlength(text, font=f)
    d.text(((W - w) / 2, y), text, font=f, fill=fill)


center_text(52, "Install Library Cleanup", font(26, bold=True), TXT)
center_text(94, "Drag the app onto the Applications folder", font(14), SUB)

# Drag arrow between the two icon slots (icons themselves are placed by dmgbuild).
y = 210
x0, x1 = 245, 385
d.line([(x0, y), (x1 - 16, y)], fill=BRAND, width=6)
d.polygon([(x1 - 18, y - 13), (x1 + 4, y), (x1 - 18, y + 13)], fill=BRAND)

center_text(330, "Then grant Full Disk Access + Photos when the app asks — see USAGE.md",
            font(12), SUB)

out = os.path.join(os.path.dirname(__file__), "..", "assets", "dmg-background.png")
os.makedirs(os.path.dirname(out), exist_ok=True)
img.save(out)
print(f"wrote {os.path.normpath(out)}")
