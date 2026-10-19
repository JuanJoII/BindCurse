"""Procedurally drawn tray icons (no external image files needed)."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

# Background colors by state
STATE_COLORS: dict[str, tuple[int, int, int, int]] = {
    "idle": (70, 78, 96, 255),  # Gray: waiting for the phone
    "streaming": (40, 175, 95, 255),  # Green: receiving audio
    "muted": (200, 55, 65, 255),  # Red: virtual mic muted
    "error": (220, 140, 30, 255),  # Orange: virtual mic not available
}


@lru_cache(maxsize=32)
def render_icon(
    state: str = "idle", size: int = 64, monitor: bool = False
) -> Image.Image:
    """Draws a round microphone icon. Rendered at 4x and downscaled for anti-aliasing."""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bg = STATE_COLORS.get(state, STATE_COLORS["idle"])
    d.ellipse((0, 0, s - 1, s - 1), fill=bg)

    white = (255, 255, 255, 255)
    u = s / 64.0  # design units on a 64x64 grid

    # Mic capsule
    d.rounded_rectangle((24 * u, 11 * u, 40 * u, 37 * u), radius=8 * u, fill=white)
    # Holder (U arc)
    d.arc(
        (17 * u, 19 * u, 47 * u, 45 * u), start=0, end=180, fill=white, width=int(4 * u)
    )
    # Stem + base
    d.rectangle((30 * u, 44 * u, 34 * u, 51 * u), fill=white)
    d.rounded_rectangle((22 * u, 50 * u, 42 * u, 54 * u), radius=2 * u, fill=white)

    if state == "muted":
        # Diagonal slash
        d.line((16 * u, 14 * u, 48 * u, 52 * u), fill=bg, width=int(9 * u))
        d.line((16 * u, 14 * u, 48 * u, 52 * u), fill=white, width=int(4 * u))

    if monitor:
        # Small blue badge = monitor (listening through speakers) enabled
        d.ellipse(
            (44 * u, 2 * u, 62 * u, 20 * u),
            fill=(60, 140, 245, 255),
            outline=white,
            width=int(2 * u),
        )

    return img.resize((size, size), Image.LANCZOS)


def to_argb32_bytes(img: Image.Image) -> bytes:
    """Converts RGBA to ARGB32 network byte order, as required by StatusNotifierItem IconPixmap."""
    r, g, b, a = img.convert("RGBA").split()
    return Image.merge("RGBA", (a, r, g, b)).tobytes()
