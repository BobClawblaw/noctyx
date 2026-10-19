"""The terminal install banner: NOCTYX in block letters, cyan to violet, with the watching eye."""
import os
import sys

GLYPHS = {
    "N": ["██▄  ██", "███▄ ██", "██ ▀███", "██   ██", "██   ██"],
    "O": [" ▄███▄ ", "██ ◉ ██", "██   ██", "██   ██", " ▀███▀ "],
    "C": [" ▄█████", "██     ", "██     ", "██     ", " ▀█████"],
    "T": ["███████", "  ███  ", "  ███  ", "  ███  ", "  ███  "],
    "Y": ["██   ██", " ██ ██ ", "  ███  ", "  ███  ", "  ███  "],
    "X": ["██   ██", " ██ ██ ", "  ███  ", " ██ ██ ", "██   ██"],
}
FROM, TO = (63, 240, 255), (194, 92, 255)          # the mark's neon: cyan -> violet
EYE = (240, 254, 255)


def _color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def render(tagline="the night-shift operator for your AI cluster", color=None):
    color = _color() if color is None else color
    rows = ["  ".join(GLYPHS[ch][r] for ch in "NOCTYX") for r in range(5)]
    width = len(rows[0])
    out = []
    for row in rows:
        line = []
        for i, ch in enumerate(row):
            if not color or ch == " ":
                line.append(ch)
                continue
            k = i / max(1, width - 1)
            r, g, b = (EYE if ch == "◉" else tuple(int(a + (z - a) * k) for a, z in zip(FROM, TO)))
            line.append(f"\033[38;2;{r};{g};{b}m{ch}")
        out.append("  " + "".join(line) + ("\033[0m" if color else ""))
    dim = "\033[38;2;143;163;200m" if color else ""
    rst = "\033[0m" if color else ""
    out.append("")
    out.append(f"  {dim}{tagline.upper().replace(' ', '  ')}{rst}")
    return "\n".join(out)


if __name__ == "__main__":
    print(render())
