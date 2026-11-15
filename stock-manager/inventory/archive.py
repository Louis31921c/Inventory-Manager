"""Old document photos, kept as packed black-and-white arrays.

A delivery note or a bill has to be kept long after the photo of it has any use as a photo,
and in France for years. A photo kept as it was taken is a megabyte or two. A document is
ink on paper, so what has to survive is where the ink is: the page is flattened to one bit
a pixel (the lighting is levelled first, so a shadow does not turn the page black), and the
grid of bits is packed eight to a byte and compressed. A 1.6 MB photo comes to about 30 KB
and the print stays readable. Colour and shading are gone, which is why a photo is kept in
full for the first weeks, and only then turned into this.

The arrays live in the database, in `photo_archive`, beside the rows they belong to, so a
backup of one file holds both. Reading one gives a PNG back.
"""

import io
import os
import zlib
from datetime import date

from . import db

LONG_SIDE = 2000        # the page is cut down to this many pixels on its long side
LEVEL_RADIUS = 18       # how wide a patch of paper is averaged to find its own white
INK_OFFSET = 14         # how much darker than its surroundings a pixel is to count as ink
DEFAULT_YEARS = 10


def retention_years():
    try:
        return max(1, int(os.environ.get("DOCUMENT_RETENTION_YEARS", DEFAULT_YEARS)))
    except ValueError:
        return DEFAULT_YEARS


def _pillow():
    from PIL import Image, ImageChops, ImageFilter, ImageOps

    return Image, ImageChops, ImageFilter, ImageOps


def pack(raw):
    """(width, height, bytes) for a photo: levelled, one bit a pixel, packed and compressed."""
    Image, ImageChops, ImageFilter, ImageOps = _pillow()
    with Image.open(io.BytesIO(raw)) as opened:
        page = ImageOps.exif_transpose(opened).convert("L")
    page.thumbnail((LONG_SIDE, LONG_SIDE))
    ground = page.filter(ImageFilter.GaussianBlur(LEVEL_RADIUS))
    ink = ImageChops.subtract(ground, page).point(lambda v: 255 if v > INK_OFFSET else 0)
    bits = ImageChops.invert(ink).convert("1", dither=Image.Dither.NONE)
    return bits.size[0], bits.size[1], zlib.compress(bits.tobytes(), 9)


def unpack(width, height, data):
    """The PNG of an archived page."""
    Image, *_ = _pillow()
    page = Image.frombytes("1", (width, height), zlib.decompress(data))
    out = io.BytesIO()
    page.save(out, "PNG", optimize=True)
    return out.getvalue()


def store(db_path, sha256, raw):
    """Pack `raw` into the archive under its hash. Returns the packed size, or None when it is
    not a picture."""
    if not sha256:
        return None
    try:
        width, height, data = pack(raw)
    except Exception:
        return None
    db.write(db_path, """
            INSERT INTO photo_archive (sha256, width, height, data, bytes_from)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sha256) DO UPDATE SET width = excluded.width, height = excluded.height,
                data = excluded.data, bytes_from = excluded.bytes_from
            RETURNING sha256""", [sha256, width, height, data, len(raw)])
    return len(data)


def get(db_path, sha256):
    """The archived page as PNG bytes, or None."""
    if not sha256:
        return None
    rows = db.read(db_path, "SELECT width, height, data FROM photo_archive WHERE sha256 = ?",
                   [sha256])
    return unpack(*rows[0]) if rows else None


def sweep(db_path, images_dir, older_than_days=0):
    """Pack every stored photo file older than `older_than_days` and drop the file. The pages
    keep their rows, which go on pointing at the hash. PDFs are left as they are."""
    packed = before = after = 0
    shots = db.read(db_path, f"""
            SELECT image_sha256, image_path FROM notes
            WHERE image_path IS NOT NULL AND image_sha256 IS NOT NULL
              AND confirmed_at < now() - INTERVAL {int(older_than_days)} DAY
            UNION
            SELECT image_sha256, image_path FROM purchase_orders
            WHERE image_path IS NOT NULL AND image_sha256 IS NOT NULL
              AND created_at < now() - INTERVAL {int(older_than_days)} DAY
            UNION
            SELECT image_sha256, image_path FROM bills
            WHERE image_path IS NOT NULL AND image_sha256 IS NOT NULL
              AND added_at < now() - INTERVAL {int(older_than_days)} DAY""")
    for sha, path in shots:
        if not path or str(path).lower().endswith(".pdf") or not os.path.isfile(path):
            continue
        raw = open(path, "rb").read()
        size = store(db_path, sha, raw)
        if size is None:
            continue
        before += len(raw)
        after += size
        packed += 1
        os.unlink(path)
        for table in ("notes", "purchase_orders", "bills"):
            db.write(db_path, f"UPDATE {table} SET image_path = NULL WHERE image_path = ? "
                              "RETURNING 1", [path])
    return {"pages": packed, "before": before, "after": after}


def forget_old(db_path, years=None, today=None):
    """Drop archived pages once the paper they belong to is past its retention."""
    years = years or retention_years()
    cut = (today or date.today()).replace(year=(today or date.today()).year - years)
    gone = db.read(db_path, "DELETE FROM photo_archive WHERE archived_on < ? RETURNING sha256", [cut])
    return len(gone)


def summary(db_path):
    rows = db.read(db_path, "SELECT count(*), coalesce(sum(octet_length(data)), 0), "
                            "coalesce(sum(bytes_from), 0) FROM photo_archive")
    count, packed, original = rows[0]
    return {"pages": int(count), "bytes": int(packed), "from_bytes": int(original),
            "years": retention_years()}
