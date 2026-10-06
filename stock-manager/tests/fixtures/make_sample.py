"""Generate a fake delivery note photo for manual testing: python tests/fixtures/make_sample.py

The result (bon_sample.jpg) is a plausible-looking but entirely invented note, slightly rotated and
blurred the way a phone photo is. It is what the corpus case `sample_note` expects, so the reading
check can run without any real company document.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LINES = [
    (34, "ACME BUILDING SUPPLIES - Northgate branch"),
    (26, "DELIVERY NOTE No. DN-2026-48213"),
    (22, ""),
    (22, "Delivery date : 18/09/2026"),
    (22, "Order date : 11/09/2026     Order ref. : PO-7781"),
    (22, "Your reference : Lime Tree Court, 12 Garibaldi Street"),
    (22, "Work item : Upper slab, level 1"),
    (22, ""),
    (20, "Ref.      Description                              Ordered   Delivered"),
    (20, "CEM35     Cement CEM II 32.5 bag 35kg                 40          40"),
    (20, "MESH25    Welded mesh ST25C 6x2.4m                    12           8     B/O"),
    (20, "SAND04    Sand 0/4 bulk bag                            3           3"),
    (20, "REB10     Rebar 10mm bar 6m                           50          50"),
    (20, "SHEET200  Polythene sheet 200um roll                   2           0     B/O"),
    (20, ""),
    (18, "B/O = balance still to deliver"),
    (20, "Customer signature :                     Driver : M. Garnier"),
]

img = Image.new("RGB", (1100, 820), "white")
draw = ImageDraw.Draw(img)
y = 50
for size, text in LINES:
    draw.text((50, y), text, fill="black", font=ImageFont.truetype(FONT, size))
    y += size + 22
img = img.rotate(1.5, expand=True, fillcolor=(235, 232, 225)).filter(ImageFilter.GaussianBlur(0.7))
out = Path(__file__).with_name("bon_sample.jpg")
img.save(out, quality=80)
print(out)
