"""Video Frame Extractor desktop application.

Run with:  python main.py
"""

import sys


def main() -> None:
    try:
        from vfe.gui import main as run_gui
    except ModuleNotFoundError as exc:
        if exc.name == "tkinter":
            sys.exit(
                "Tkinter is not available in this Python installation.\n"
                "Windows/macOS: reinstall Python from python.org with Tcl/Tk enabled.\n"
                "Ubuntu/Debian: sudo apt install python3-tk\n"
                "Fedora: sudo dnf install python3-tkinter"
            )
        raise
    run_gui()


if __name__ == "__main__":
    main()
