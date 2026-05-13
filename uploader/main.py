"""Frozen-app entry point.

PyInstaller can't run a module-relative file as `__main__` and keep relative imports
working, so this thin wrapper imports the real entry through the `src` package.
"""

import sys

if "--settings" in sys.argv:
    from src.settings_window import main as settings_main

    settings_main()
    sys.exit(0)


from src.tray_app import main


if __name__ == "__main__":
    sys.exit(main())
