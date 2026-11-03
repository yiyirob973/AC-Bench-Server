#!/usr/bin/env python3
"""Windows-only helper: finds the Assetto Corsa window and presses AC's "Start race" hotkey in it (Ctrl + the key set in
controls.ini, see ac_controls.py). ac_lap_bench.py uses it to get past the pre-race screen. It's the only way the app
starts a race, so there is no click position to calibrate.

  python ac_input.py hotkey [--docs-dir "<Documents/Assetto Corsa>"]
      test: press the Start race hotkey once (AC open on the pre-race screen)

The keys go to whatever window is in front, so leave the PC alone while a benchmark runs.
"""
import sys
import time
from pathlib import Path

import ac_paths  # noqa: F401  (keeps the packaged build's module list complete)

TITLE = "Assetto Corsa"
CTRL = 0x11
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}   # these keys need the extended-key flag

try:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
except (ImportError, AttributeError, OSError):
    user32 = None


def _need_windows():
    if user32 is None:
        raise RuntimeError("Starting races from the app only works on Windows.")


def find_window(title=TITLE):
    """Handle of the visible window with exactly this title, or None."""
    _need_windows()
    found = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == title:
                    found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    return found[0] if found else None


def focus(hwnd):
    user32.keybd_event(0x12, 0, 0, 0)       # tapping Alt lets us take the foreground
    user32.keybd_event(0x12, 0, 2, 0)
    user32.ShowWindow(hwnd, 9)              # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def _key_event(vk, up=False):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = (1 if vk in EXTENDED else 0) | (2 if up else 0)
    user32.keybd_event(vk, scan, flags, 0)


def press_combo(hwnd, vks):
    """Hold the modifiers, tap the last key, release in reverse order, like a person pressing Ctrl+F12."""
    focus(hwnd)
    for vk in vks:
        _key_event(vk)
        time.sleep(0.05)
    time.sleep(0.12)
    for vk in reversed(vks):
        _key_event(vk, up=True)
        time.sleep(0.03)


def start_race(vk):
    """Press Ctrl+<vk> in the AC window. Returns False if there is no AC window yet."""
    hwnd = find_window()
    if not hwnd:
        return False
    press_combo(hwnd, [CTRL, vk])
    return True


def main():
    ac_paths.utf8_output()
    if len(sys.argv) > 1 and sys.argv[1] == "hotkey":
        import ac_controls
        docs = sys.argv[sys.argv.index("--docs-dir") + 1] if "--docs-dir" in sys.argv else str(
            Path.home() / "Documents" / "Assetto Corsa")
        try:
            vk, note = ac_controls.ensure_start_hotkey(docs)
            print(note)
            if vk is None:
                return 1
            print("Sent Ctrl+%s." % ac_controls.vk_name(vk) if start_race(vk)
                  else f"No '{TITLE}' window found. Open AC (on the pre-race screen) first.")
            return 0
        except Exception as e:
            print("Error:", e)
            return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
