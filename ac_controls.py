#!/usr/bin/env python3
"""Starts a race with AC's own "Start race" command instead of clicking the Drive button.

Content Manager > Settings > Assetto Corsa > Controls > System > Race has a "Start race" row. It's stored in
Documents/Assetto Corsa/cfg/controls.ini as

    [__CM_START_SESSION]
    KEY=<Windows virtual-key code>

and the hotkey is Ctrl + that key. This module makes sure the entry exists (so nobody has to set it up in Content
Manager) and tells ac_input.py which key to press. A binding the user already has is kept.

Who handles the hotkey in the game: Custom Shaders Patch handles the hotkeys listed in
<AC>/extension/config/data_hotkeys_supported.txt itself. Content Manager handles the rest, but only for games it
launched. csp_handles_start_session() tells you which case this install is.

  python ac_controls.py status --docs-dir "<Documents/Assetto Corsa>" [--ac-dir "<AC>"]
  python ac_controls.py setup  --docs-dir "<Documents/Assetto Corsa>" [--ac-dir "<AC>"]
"""
import argparse
import re
import sys
from pathlib import Path

import ac_paths  # noqa: F401  (keeps the packaged build's module list complete)

SECTION = "__CM_START_SESSION"
DEFAULT_VK = 0x7B            # F12 isn't a toggle key and nothing else in AC uses Ctrl+F12
BACKUP_NAME = "controls.ini.acbench.bak"
_HEADER = re.compile(r"^\s*\[([^\]]*)\]\s*$")
# Content Manager writes the key as hex followed by its name, e.g. KEY=0x4F ; O (older tools write KEY=79)
_KEY = re.compile(r"^\s*KEY\s*=\s*(-?(?:0[xX][0-9A-Fa-f]+|\d+))\s*(?:[;#].*)?$", re.IGNORECASE)
# Ctrl + these letters are also AC shortcuts (ABS, TC, hide apps, restart session, names, mouse steering, auto gearbox,
# racing line, replay, slow motion, virtual desktops). A key the user chose is still used, the log just mentions it.
BUILTIN_CTRL = {ord(c): n for c, n in (("A", "ABS"), ("T", "traction control"), ("H", "hide apps"), ("O", "restart session"),
                                       ("L", "driver names"), ("M", "mouse steering"), ("G", "automatic gearbox"),
                                       ("I", "racing line"), ("R", "replay"), ("S", "slow motion"), ("U", "virtual desktops"))}

_NAMES = {0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space", 0x21: "Page Up", 0x22: "Page Down",
          0x23: "End", 0x24: "Home", 0x2D: "Insert", 0x2E: "Delete", 0x13: "Pause", 0x91: "Scroll Lock"}


def vk_name(vk):
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return "Numpad %d" % (vk - 0x60)
    return _NAMES.get(vk, "key %d" % vk)


def controls_ini(docs_dir):
    return Path(docs_dir) / "cfg" / "controls.ini"


def _split(text):
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.split(nl), nl


def _find_section(lines, name):
    """(header index, index of the next header or len) of the first [name] section, or None."""
    start = None
    for i, line in enumerate(lines):
        m = _HEADER.match(line.rstrip("\r"))
        if m:
            if start is not None:
                return start, i
            if m.group(1).strip().upper() == name.upper():
                start = i
    return (start, len(lines)) if start is not None else None


def _to_int(text):
    return int(text, 16) if text.lower().lstrip("-").startswith("0x") else int(text)


def _find_keys(lines, start, end):
    """[(line index, value)] for every KEY= line in the section (decimal or 0x hex, optional comment)."""
    out = []
    for i in range(start + 1, end):
        m = _KEY.match(lines[i].rstrip("\r"))
        if m:
            out.append((i, _to_int(m.group(1))))
    return out


def _effective(keys):
    """The key that counts. With duplicate KEY lines the last valid one wins, like any ini reader."""
    good = [v for _, v in keys if v > 0]
    return good[-1] if good else None


def _canonical(vk):
    return "KEY=0x%02X ; %s" % (vk, vk_name(vk))      # same format Content Manager writes


def read_start_hotkey(docs_dir):
    """The Ctrl+<key> virtual-key code set for 'Start race' in controls.ini, or None."""
    path = controls_ini(docs_dir)
    try:
        text = path.read_bytes().decode("latin-1")
    except OSError:
        return None
    lines, _ = _split(text)
    sec = _find_section(lines, SECTION)
    if not sec:
        return None
    return _effective(_find_keys(lines, *sec))


def ensure_start_hotkey(docs_dir, default_vk=DEFAULT_VK, say=print):
    """Make sure 'Start race' has a usable key in controls.ini and return it.
    - A binding that's already there is always used and never changed (Content Manager writes it as hex with a comment,
      e.g. KEY=0x4F ; O).
    - Duplicate KEY lines are cleaned up so exactly one is left.
    - Only when nothing is set, Ctrl+F12 is written, the way Content Manager writes it.
    Only this one section is touched. The rest of the file is kept byte for byte and a backup of the original is saved once
    as controls.ini.acbench.bak. Returns (virtual-key code or None, what happened).
    """
    path = controls_ini(docs_dir)
    if not path.is_file():
        return None, ("controls.ini not found in %s: start Assetto Corsa once (or Content Manager) so it is created"
                      % path.parent)
    original = path.read_bytes()
    lines, nl = _split(original.decode("latin-1"))
    sec = _find_section(lines, SECTION)
    before = " | ".join(x.rstrip("\r") for x in lines[sec[0]:sec[1]] if x.strip()) if sec else ""
    notes = ""
    if sec:
        keys = _find_keys(lines, *sec)
        eff = _effective(keys)
        if eff:
            vk, how = eff, "using the 'Start race' hotkey set in controls.ini: Ctrl+%s" % vk_name(eff)
            if eff in BUILTIN_CTRL:
                how += " (note: AC also uses Ctrl+%s for %s)" % (vk_name(eff), BUILTIN_CTRL[eff])
            keep_line = [i for i, v in keys if v == eff][-1]
            drop = [i for i, _ in keys if i != keep_line]
            if not drop:
                return vk, how
            how += " (removed %d duplicate KEY line%s)" % (len(drop), "s" if len(drop) > 1 else "")
            for i in sorted(drop, reverse=True):
                del lines[i]
        else:
            vk = default_vk
            how = "nothing was set: set the 'Start race' hotkey in controls.ini to Ctrl+%s" % vk_name(vk)
            if keys:
                first = keys[0][0]
                lines[first] = _canonical(vk)
                for i in sorted((i for i, _ in keys[1:]), reverse=True):
                    del lines[i]
            else:
                lines.insert(sec[0] + 1, _canonical(vk))
        how += " (the section before: %s)" % before
    else:
        vk = default_vk
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines += ["", "[%s]" % SECTION, _canonical(vk), "JOY=-1", "BUTTON=-1", "BUTTON_MODIFICATOR=-1", ""]
        how = "set the 'Start race' hotkey in controls.ini: Ctrl+%s (there was no section)" % vk_name(vk)
    backup = path.with_name(BACKUP_NAME)
    try:
        if not backup.exists():
            backup.write_bytes(original)
        tmp = path.with_name(path.name + ".acbench.tmp")
        tmp.write_bytes(nl.join(lines).encode("latin-1"))
        tmp.replace(path)
    except OSError as e:
        return None, "could not write controls.ini (%s)" % e
    return vk, how


def raw_section(docs_dir):
    """The lines of [__CM_START_SESSION] exactly as they are in controls.ini (header first), or None."""
    try:
        lines, _ = _split(controls_ini(docs_dir).read_bytes().decode("latin-1"))
    except OSError:
        return None
    sec = _find_section(lines, SECTION)
    return [x.rstrip("\r") for x in lines[sec[0]:sec[1]] if x.strip()] if sec else None


def content_manager_running():
    """True/False if Content Manager is running (Windows only), None if it can't be told."""
    if sys.platform != "win32":
        return None
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Content Manager.exe", "/NH"], capture_output=True,
                             text=True, timeout=15, creationflags=0x08000000).stdout
        return "content manager.exe" in out.lower()
    except Exception:
        return None


def csp_handles_start_session(ac_dir):
    """True if Custom Shaders Patch handles the 'Start race' hotkey inside the game. False if it doesn't (then only Content
    Manager does, and only for games it started). None if it can't be told (no Custom Shaders Patch file).
    """
    p = Path(ac_dir) / "extension" / "config" / "data_hotkeys_supported.txt"
    try:
        names = {x.strip() for x in p.read_text(encoding="utf-8", errors="replace").splitlines()}
    except OSError:
        return None
    return SECTION in names


def describe(docs_dir, ac_dir=None):
    vk = read_start_hotkey(docs_dir)
    lines = [("Start-race hotkey in controls.ini: Ctrl+%s" % vk_name(vk)) if vk
             else "Start-race hotkey in controls.ini: not set yet (the app sets it before the first benchmark)"]
    raw = raw_section(docs_dir)
    lines.append("controls.ini says: " + (" | ".join(raw) if raw else "(no [%s] section)" % SECTION))

    if ac_dir:
        csp = csp_handles_start_session(ac_dir)
        lines.append({True: "Custom Shaders Patch handles it inside the game: works however AC is started, Content Manager "
                            "is not needed",
                      False: "Custom Shaders Patch does NOT list it: then only Content Manager acts on this hotkey",
                      None: "Custom Shaders Patch hotkey list not found (CSP not installed or not recent enough)"}[csp])
        if csp is not True:
            cm = content_manager_running()
            lines.append({True: "Content Manager is running",
                          False: "Content Manager is NOT running, so the hotkey may do nothing",
                          None: "Content Manager: cannot tell if it is running"}[cm])
    return lines


def main(argv=None):
    ac_paths.utf8_output()
    ap = argparse.ArgumentParser(description="Set up and inspect AC's 'Start race' hotkey")
    ap.add_argument("action", choices=["status", "setup"])
    ap.add_argument("--docs-dir", default=str(Path.home() / "Documents" / "Assetto Corsa"))
    ap.add_argument("--ac-dir")
    args = ap.parse_args(argv)
    if args.action == "setup":
        vk, note = ensure_start_hotkey(args.docs_dir)
        print(note)
        if vk is None:
            return 1
    for line in describe(args.docs_dir, args.ac_dir):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
