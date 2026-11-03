# PyInstaller spec for the single portable exe:   pyinstaller --noconfirm AC-Bench.spec    (or run build_exe.bat)
# Output: dist\AC-Bench.exe. The race template, the benchmark car and the Lua logger are embedded in it; settings and
# results are written next to the exe (see ac_paths.py).
import os

for required in ("template_race.ini", "bundled_car", os.path.join("apps", "lua", "ac_bench")):
    if not os.path.exists(required):
        raise SystemExit("Cannot build: %s is missing from this folder" % required)

# the GUI starts the other scripts by re-running this exe (--ac-run <module>), so every ac_* module is embedded
hidden = ["ac_paths", "ac_lap_bench", "ac_cloud", "ac_net", "ac_conditions", "ac_live", "ac_tag_writer", "ac_track_check",
          "ac_baseline", "ac_rating", "ac_carid", "ac_car_classifier", "ac_input", "ac_controls", "ac_classify_laps",
          "ac_grid_builder", "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog", "tkinter.font"]

a = Analysis(
    ["ac_bench_gui.py"],
    pathex=["."],
    datas=[("template_race.ini", "."),
           ("bundled_car", "bundled_car"),
           ("apps", "apps")],
    hiddenimports=hidden,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name="AC-Bench",
          console=False,      # windowed; child jobs get their output through pipes
          upx=False)
