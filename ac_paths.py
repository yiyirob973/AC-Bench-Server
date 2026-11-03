"""Where the app's files live, from source and in the packaged Nuitka build (standalone or onefile).

RES   read-only files shipped inside the app: template_race.ini, bundled_car/, apps/lua/ac_bench/. In the exe they are
      unpacked to a temporary folder that is deleted when the app closes, so nothing that has to stay may be written
      there.
DATA  everything the app writes and keeps (settings, results, cloud_config.json, caches, logs). That's the folder the
      exe sits in, so copying the folder moves everything. If it isn't writable (Program Files, say) the app falls back
      to %LOCALAPPDATA%\\AC-Bench.
From source, both are just the folder with the .py files.
"""
import os
import sys
from pathlib import Path

# every Nuitka-compiled module has __compiled__
FROZEN = "__compiled__" in globals()
APP_NAME = "AC-Bench"


def _writable(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _exe_folder():
    """Folder the user's exe sits in (for a Nuitka onefile build that's the original exe, not the unpack folder)."""
    compiled = globals().get("__compiled__")
    containing = getattr(compiled, "containing_dir", None)
    if containing:
        return Path(containing).resolve()
    if compiled is not None and sys.argv and sys.argv[0]:     # Nuitka: argv[0] is the exe the user started
        started = Path(sys.argv[0])
        if started.is_absolute() and started.is_file():
            return started.resolve().parent
    return Path(sys.executable).resolve().parent


def _data_dir():
    forced = os.environ.get("AC_BENCH_DATA")    # set by the app for the jobs it starts, so they use the same folder as the window
    if forced and Path(forced).is_dir():
        return Path(forced).resolve()
    if not FROZEN:
        return Path(__file__).resolve().parent
    beside_exe = _exe_folder()
    if _writable(beside_exe):
        return beside_exe
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    fallback = Path(base) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


RES = Path(__file__).resolve().parent
DATA = _data_dir()


def utf8_output():
    """Make this process write UTF-8 and never fail on a character. The app reads every job's output as UTF-8, but Windows
    gives a piped child the console code page (cp1252), which can't hold car names like 'Golf Mk⁶'.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")          # a real console keeps its own encoding
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def bundled_car_id():
    """Folder name of the car shipped in bundled_car/ (the benchmark car), or None if there isn't exactly one."""
    try:
        dirs = [p for p in (RES / "bundled_car").iterdir() if p.is_dir()]
    except OSError:
        return None
    return dirs[0].name if len(dirs) == 1 else None


def app_executable():
    """The program to start again for a job (a packaged build runs its jobs as copies of itself). sys.executable isn't
    reliable in a one-file build and can name a file that doesn't exist, so ask Windows for the running process's image
    first, then fall back to sys.executable and the path the user started.
    """
    candidates = []
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
                candidates.append(buf.value)
        except Exception:
            pass
    candidates += [sys.executable, sys.argv[0] if sys.argv else ""]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return sys.executable


def main():
    """Show where this build looks for its files (python ac_paths.py, or the exe with --ac-run ac_paths.py)."""
    print("frozen=%s\nRES=%s\nDATA=%s\nEXE=%s" % (FROZEN, RES, DATA, app_executable()))


def data(name):
    """A file or folder that belongs to the user (settings, results, caches)."""
    return DATA / name


def res(*parts):
    """A file or folder shipped inside the app (template, bundled car, Lua logger)."""
    return RES.joinpath(*parts)


if __name__ == "__main__":
    main()
