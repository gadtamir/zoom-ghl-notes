"""Generate the tray icon programmatically — no asset file needed in PyInstaller bundle."""

from PIL import Image, ImageDraw


def make_icon(size: int = 64, active: bool = True) -> Image.Image:
    """Solid circle with a 'Z' inside. Color: blue when active, gray when paused."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (37, 99, 235, 255) if active else (113, 113, 122, 255)  # blue-600 / zinc-500
    d.ellipse((2, 2, size - 2, size - 2), fill=fill)

    # Draw a thick "Z" inside.
    w = size
    mx = w // 5
    my = w // 5
    top = my
    bottom = w - my
    left = mx
    right = w - mx
    thickness = max(3, w // 12)
    white = (255, 255, 255, 255)
    # top horizontal
    d.rectangle((left, top, right, top + thickness), fill=white)
    # diagonal
    d.line((right, top + thickness, left, bottom - thickness), fill=white, width=thickness)
    # bottom horizontal
    d.rectangle((left, bottom - thickness, right, bottom), fill=white)
    return img
