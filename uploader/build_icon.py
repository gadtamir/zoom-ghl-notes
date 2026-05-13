"""Generate platform icons (.icns for macOS, .ico for Windows) from the in-code icon.

Run once before building: `python build_icon.py`
Outputs:
  build_assets/icon.icns  (macOS)
  build_assets/icon.ico   (Windows)
  build_assets/icon.png   (universal 512×512)
"""

import subprocess
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from src.icon import make_icon


ASSETS = HERE / "build_assets"
ASSETS.mkdir(exist_ok=True)


def _save_png_512() -> Path:
    img = make_icon(size=1024, active=True)  # render high-res
    path = ASSETS / "icon.png"
    img.save(path, format="PNG")
    return path


def _save_ico() -> Path:
    """Windows .ico — multi-size."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = make_icon(size=256, active=True)
    path = ASSETS / "icon.ico"
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    return path


def _save_icns() -> Path:
    """macOS .icns — requires iconutil (Xcode toolchain, ships with Xcode CLT)."""
    iconset_dir = ASSETS / "icon.iconset"
    iconset_dir.mkdir(exist_ok=True)

    sizes = [
        (16, "16x16"),
        (32, "16x16@2x"),
        (32, "32x32"),
        (64, "32x32@2x"),
        (128, "128x128"),
        (256, "128x128@2x"),
        (256, "256x256"),
        (512, "256x256@2x"),
        (512, "512x512"),
        (1024, "512x512@2x"),
    ]
    for px, name in sizes:
        img = make_icon(size=px, active=True)
        img.save(iconset_dir / f"icon_{name}.png", format="PNG")

    icns_path = ASSETS / "icon.icns"
    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)], check=True)
    except FileNotFoundError:
        print("iconutil not found (Xcode CLT required for .icns)")
        return ASSETS / "icon.png"
    return icns_path


if __name__ == "__main__":
    png = _save_png_512()
    print(f"✓ {png}")
    ico = _save_ico()
    print(f"✓ {ico}")
    icns = _save_icns()
    print(f"✓ {icns}")
