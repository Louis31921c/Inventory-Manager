"""A character grid the app draws into, painted by curses or saved as an image."""

NORMAL = "normal"
DIM = "dim"
BRIGHT = "bright"
INVERT = "invert"
TAB = "tab"
TAB_ON = "tab_on"
KEY = "key"
WARN = "warn"

STYLES = (NORMAL, DIM, BRIGHT, INVERT, TAB, TAB_ON, KEY, WARN)


class Screen:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.clear()

    def clear(self):
        self.cells = [[(" ", NORMAL) for _ in range(self.width)] for _ in range(self.height)]

    def put(self, row, col, text, style=NORMAL):
        if row < 0 or row >= self.height:
            return
        for offset, char in enumerate(str(text)):
            at = col + offset
            if 0 <= at < self.width:
                self.cells[row][at] = (char, style)

    def right(self, row, col_end, text, style=NORMAL):
        self.put(row, max(0, col_end - len(str(text))), text, style)

    def rule(self, row, col=0, width=None, char="-", style=DIM):
        self.put(row, col, char * (width or (self.width - col)), style)

    def rows_text(self):
        return ["".join(char for char, _ in row).rstrip() for row in self.cells]

    def __str__(self):
        return "\n".join(self.rows_text())


def truncate(text, width):
    text = "" if text is None else str(text)
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "~"


def pad(text, width):
    return truncate(text, width).ljust(width)


def rpad(text, width):
    return truncate(text, width).rjust(width)


def columns(widths, values, gap=2):
    out = []
    for width, value in zip(widths, values):
        out.append(rpad(value, -width) if width < 0 else pad(value, width))
    return (" " * gap).join(out)
