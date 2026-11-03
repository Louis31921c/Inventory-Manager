"""A photo of a delivery note is kept small: the paper has to stay readable, no more.

The model reads the photo as it was taken. What is kept afterwards is only the proof
that the paper existed, so it is turned upright, cut down to 1600 pixels on its long
side and written again at a quality where print is still sharp. A 2 MB phone photo comes
to about 250 KB. It is not stored as a grid of numbers: a raw pixel array is far larger
than the file it came from (a 12 megapixel photo is 36 MB of numbers), the saving is in
the cutting down and the compression, and neither needs anything outside this machine.
The metadata goes with the rest, which takes the place where the photo was taken with it.
"""

import io
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # the photo is then kept as it came
    Image = None

LONG_SIDE = 1600
QUALITY = 72
SHRINKABLE = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


def shrink(raw, suffix):
    """The bytes to keep for `raw`, or `raw` itself when it cannot be made smaller."""
    kind = SHRINKABLE.get(suffix.lower())
    if Image is None or kind is None:
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            picture = ImageOps.exif_transpose(opened)
            picture.thumbnail((LONG_SIDE, LONG_SIDE))
            out = io.BytesIO()
            if kind == "JPEG":
                picture.convert("RGB").save(out, "JPEG", quality=QUALITY, optimize=True,
                                            progressive=True)
            elif kind == "WEBP":
                picture.save(out, "WEBP", quality=QUALITY)
            else:
                picture.save(out, "PNG", optimize=True)
    except Exception:
        return raw
    return out.getvalue() if 0 < out.tell() < len(raw) else raw


def keep(path, raw):
    """Write the photo to `path`, shrunk. Gives back the bytes written."""
    path = Path(path)
    data = shrink(raw, path.suffix)
    path.write_bytes(data)
    return len(data)


def shrink_folder(folder):
    """Shrink every photo already in a folder, in place. Same names, so nothing that
    points at them has to change."""
    before = after = count = 0
    for path in sorted(Path(folder).glob("*")):
        if path.suffix.lower() not in SHRINKABLE or not path.is_file():
            continue
        raw = path.read_bytes()
        data = shrink(raw, path.suffix)
        before += len(raw)
        if len(data) < len(raw):
            path.write_bytes(data)
            count += 1
        after += min(len(data), len(raw))
    return {"files": count, "before": before, "after": after}
