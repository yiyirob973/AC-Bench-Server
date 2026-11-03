"""Colors and look of the app. The palette comes from the AC Bench logo (dark navy badge, blue ring, cyan accents).

Everything that decides how the window looks lives here, so the GUI only has to call use_theme() and apply().
"""
import ac_paths

# Surfaces: three close steps of the logo's navy, so the window reads as one calm block instead of patches of color
BG = "#101a2b"          # window and tab pages
BG_RAISED = "#16233a"   # buttons, tab strip, list headers
BG_FIELD = "#0b1422"    # inputs, lists and the log
ROW_ALT = "#0e182a"     # every other row in the car lists (barely different on purpose)
BORDER = "#25374f"      # hairlines; quiet on purpose, the logo's brighter ring blue is only used for focus
FOCUS = "#3f8da6"       # the outline of the field or button you're working in

# Accent: the logo's cyan, softened. Only the main action, the selected row/tab and progress use it.
ACCENT = "#5cc8d8"
ACCENT_HOVER = "#7ad8e5"
ACCENT_DIM = "#1b3a52"      # selected rows, hover on normal buttons

# Text: off-white and two greys. Hierarchy comes from these, not from color.
TEXT = "#e6edf5"
TEXT_MUTED = "#8fa3b8"
TEXT_DISABLED = "#55677d"

# Status colors for the lists: muted so a long list doesn't look like a traffic light
OK = "#6bbf9a"
WARN = "#d9b062"
ERROR = "#e07a7a"

ICON_ICO = ac_paths.res("branding", "ac-bench.ico")
ICON_PNG = ac_paths.res("branding", "ac-bench.png")
HEADER_PNG = ac_paths.res("branding", "header.png")


def use_theme(root, style):
    """Switch to the theme that can be recolored. The native Windows theme ignores most colors, so it isn't used."""
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(background=BG)


def set_window_icon(root):
    """The logo in the title bar and the taskbar. Returns the PhotoImage objects, which have to be kept alive."""
    import tkinter as tk
    keep = []
    try:
        if ICON_ICO.is_file():
            root.iconbitmap(default=str(ICON_ICO))
    except tk.TclError:
        pass
    try:
        if ICON_PNG.is_file():
            img = tk.PhotoImage(file=str(ICON_PNG))
            root.iconphoto(True, img)
            keep.append(img)
    except tk.TclError:
        pass
    return keep


def header_logo():
    """The small logo for the strip at the top of the window, or None if the file is missing."""
    import tkinter as tk
    try:
        return tk.PhotoImage(file=str(HEADER_PNG)) if HEADER_PNG.is_file() else None
    except tk.TclError:
        return None


def apply(root, style, bold_font):
    """Colors for every widget the app uses. Call it before any widget is created. Everything is flat: no bevels."""
    # plain tk widgets (they don't read ttk styles)
    for pattern, value in (
        ("*Toplevel.background", BG), ("*Frame.background", BG), ("*Canvas.background", BG),
        ("*Text.background", BG_FIELD), ("*Text.foreground", TEXT), ("*Text.insertBackground", TEXT),
        ("*Text.selectBackground", ACCENT_DIM), ("*Text.selectForeground", TEXT),
        ("*Text.highlightThickness", 1), ("*Text.highlightBackground", BORDER), ("*Text.highlightColor", FOCUS),
        ("*Text.borderWidth", 0), ("*Text.padX", 6), ("*Text.padY", 4),
        ("*Listbox.background", BG_FIELD), ("*Listbox.foreground", TEXT),
        ("*Listbox.selectBackground", ACCENT_DIM), ("*Listbox.selectForeground", TEXT),
        ("*Listbox.highlightThickness", 0), ("*Listbox.borderWidth", 0),
        ("*TCombobox*Listbox.background", BG_FIELD), ("*TCombobox*Listbox.foreground", TEXT),
        ("*TCombobox*Listbox.selectBackground", ACCENT_DIM), ("*TCombobox*Listbox.selectForeground", TEXT),
    ):
        root.option_add(pattern, value)

    # clam draws a light and a dark edge for a 3D look; making them the border color flattens everything
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=BG_FIELD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, troughcolor=BG_FIELD, focuscolor=FOCUS,
                    insertcolor=TEXT, selectbackground=ACCENT_DIM, selectforeground=TEXT)
    style.map(".", foreground=[("disabled", TEXT_DISABLED)])

    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=TEXT_MUTED)
    style.configure("Bold.TLabel", font=bold_font)
    style.configure("Header.TLabel", background=BG, foreground=TEXT)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=bold_font)
    style.configure("TSeparator", background=BORDER)

    # buttons: flat panels with a hairline; the one main action per tab is solid accent
    style.configure("TButton", background=BG_RAISED, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BG_RAISED, darkcolor=BG_RAISED, focusthickness=1, focuscolor=FOCUS, relief="flat")
    style.map("TButton",
              background=[("disabled", BG), ("pressed", ACCENT_DIM), ("active", ACCENT_DIM)],
              lightcolor=[("pressed", ACCENT_DIM), ("active", ACCENT_DIM)],
              darkcolor=[("pressed", ACCENT_DIM), ("active", ACCENT_DIM)],
              bordercolor=[("disabled", BORDER), ("focus", FOCUS), ("active", FOCUS)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#08121f", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, focuscolor="#08121f")
    style.map("Accent.TButton",
              background=[("disabled", BG_RAISED), ("pressed", ACCENT), ("active", ACCENT_HOVER)],
              lightcolor=[("disabled", BG_RAISED), ("active", ACCENT_HOVER)],
              darkcolor=[("disabled", BG_RAISED), ("active", ACCENT_HOVER)],
              bordercolor=[("disabled", BORDER), ("active", ACCENT_HOVER)],
              foreground=[("disabled", TEXT_DISABLED)])

    # tabs: the strip is a shade darker than the page; the open tab merges into the page and turns white
    style.configure("TNotebook", background=BG_FIELD, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=BG_FIELD, foreground=TEXT_MUTED, bordercolor=BORDER,
                    lightcolor=BG_FIELD, darkcolor=BG_FIELD)
    style.map("TNotebook.Tab",
              background=[("selected", BG), ("active", BG_RAISED)],
              lightcolor=[("selected", BG), ("active", BG_RAISED)],
              darkcolor=[("selected", BG), ("active", BG_RAISED)],
              foreground=[("selected", TEXT), ("active", TEXT)])

    # lists: quiet headers in muted text, soft selection, hairline between rows
    style.configure("Treeview", background=BG_FIELD, fieldbackground=BG_FIELD, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
    style.map("Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=BG_RAISED, foreground=TEXT_MUTED, bordercolor=BORDER,
                    lightcolor=BG_RAISED, darkcolor=BG_RAISED, relief="flat", font=bold_font)
    style.map("Treeview.Heading", background=[("active", ACCENT_DIM)], foreground=[("active", TEXT)])

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=BG_FIELD, foreground=TEXT, bordercolor=BORDER, insertcolor=TEXT,
                        lightcolor=BG_FIELD, darkcolor=BG_FIELD, arrowcolor=TEXT_MUTED, background=BG_RAISED,
                        padding=3)
        style.map(name, bordercolor=[("focus", FOCUS), ("hover", FOCUS)],
                  lightcolor=[("focus", FOCUS)], darkcolor=[("focus", FOCUS)],
                  fieldbackground=[("disabled", BG), ("readonly", BG_FIELD)],
                  foreground=[("disabled", TEXT_DISABLED)], arrowcolor=[("active", TEXT)])

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=BG, foreground=TEXT, indicatorbackground=BG_FIELD,
                        indicatorforeground="#08121f", upperbordercolor=BORDER, lowerbordercolor=BORDER)
        style.map(name, background=[("active", BG)],
                  indicatorbackground=[("selected", ACCENT), ("active", BG_RAISED)],
                  upperbordercolor=[("focus", FOCUS)], lowerbordercolor=[("focus", FOCUS)],
                  foreground=[("disabled", TEXT_DISABLED)])

    # scrollbars and progress: thin and low contrast
    style.configure("TScrollbar", background="#22344d", troughcolor=BG, bordercolor=BG, lightcolor="#22344d",
                    darkcolor="#22344d", arrowcolor=TEXT_MUTED, gripcount=0)
    style.map("TScrollbar", background=[("active", "#2e4666"), ("pressed", "#2e4666")],
              lightcolor=[("active", "#2e4666")], darkcolor=[("active", "#2e4666")])
    style.configure("TProgressbar", background=ACCENT, troughcolor=BG_FIELD, bordercolor=BORDER,
                    lightcolor=ACCENT, darkcolor=ACCENT)
