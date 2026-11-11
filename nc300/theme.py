"""Native desktop styling: quiet surfaces, clear hierarchy, one accent."""
from tkinter import ttk
from pathlib import Path
import ctypes
from ctypes import wintypes
import sys

BG = "#101116"
PANEL = "#191c25"
FIELD = "#272b38"
TEXT = "#f0f2f8"
MUTED = "#969bad"
ACCENT = "#a3b1ff"
ERROR = "#ffacb1"


def apply_theme(root):
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10),
                    bordercolor=BG, lightcolor=BG, darkcolor=BG)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=PANEL)
    for name, options in {
        "TLabel": {},
        "Muted.TLabel": {"foreground": MUTED},
        "Card.TLabel": {"background": PANEL},
        "CardMuted.TLabel": {"background": PANEL, "foreground": MUTED},
        "Title.TLabel": {"font": ("Segoe UI", 24, "bold")},
        "Eyebrow.TLabel": {"foreground": MUTED, "font": ("Segoe UI", 9, "bold")},
        "Heading.TLabel": {"font": ("Segoe UI", 16, "bold")},
        "CardHeading.TLabel": {"background": PANEL, "font": ("Segoe UI", 12, "bold")},
    }.items():
        style.configure(name, **{"background": BG, "foreground": TEXT, **options})
    style.layout("TButton", [("Button.padding", {"sticky": "nswe", "children": [
        ("Button.label", {"sticky": "nswe"})]})])
    style.configure("TButton", background=FIELD, foreground=TEXT, padding=(15, 10),
                    font=("Segoe UI", 10), borderwidth=0, focusthickness=0)
    style.map("TButton", background=[("disabled", "#20232d"), ("pressed", "#34394c"),
                                      ("active", "#323747"), ("focus", "#323747")],
              foreground=[("disabled", "#626879")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#161a2b",
                    font=("Segoe UI", 10, "bold"), padding=(20, 11))
    style.map("Accent.TButton", background=[("disabled", "#292e43"), ("pressed", "#8899ed"),
                                             ("active", "#bbc5ff")],
              foreground=[("disabled", "#737b9c")])
    style.configure("Nav.TButton", background=BG, foreground=MUTED,
                    font=("Segoe UI", 11), padding=(23, 11))
    style.map("Nav.TButton", background=[("active", PANEL)], foreground=[("active", TEXT)])
    style.configure("Selected.Nav.TButton", background=FIELD, foreground=TEXT,
                    font=("Segoe UI", 11, "bold"))
    style.map("Selected.Nav.TButton", background=[("active", "#323747")], foreground=[("active", TEXT)])
    style.configure("Control.TNotebook", background=BG, borderwidth=0, tabmargins=0)
    style.layout("Control.TNotebook.Tab", [])
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                    arrowcolor=MUTED, padding=(10, 8), bordercolor=FIELD,
                    lightcolor=FIELD, darkcolor=FIELD, selectbackground=FIELD,
                    selectforeground=TEXT, borderwidth=0)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("disabled", "#646b7d"), ("readonly", TEXT)],
              bordercolor=[("focus", ACCENT), ("readonly", FIELD)])
    style.configure("TCheckbutton", background=BG, foreground=MUTED,
                    indicatorbackground=FIELD, indicatorforeground=ACCENT, padding=3)
    style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT)],
              indicatorbackground=[("selected", ACCENT), ("active", "#3c435a")])
    style.configure("Card.TCheckbutton", background=PANEL)
    style.map("Card.TCheckbutton", background=[("active", PANEL)])
    style.configure("Horizontal.TScale", background=PANEL, troughcolor="#0f121b",
                    sliderlength=18, sliderthickness=14, borderwidth=0)
    style.configure("Horizontal.TProgressbar", troughcolor="#10131c", background=ACCENT,
                    borderwidth=0, thickness=5, lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=33, borderwidth=0, bordercolor=PANEL)
    style.configure("Treeview.Heading", background=FIELD, foreground=MUTED,
                    font=("Segoe UI", 9, "bold"), padding=(12, 9), relief="flat")
    style.map("Treeview", background=[("selected", "#333c5c")], foreground=[("selected", TEXT)])
    style.configure("Vertical.TScrollbar", background="#3b4051", troughcolor=PANEL,
                    bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL,
                    arrowcolor=PANEL, arrowsize=0, width=7, gripcount=0)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", "#3c466b")
    if sys.platform == "win32":
        root.iconbitmap(default=str(Path(__file__).parent / "assets/app.ico"))
        root.update_idletasks()
        user32 = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        window = user32.GetParent(root.winfo_id())
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        for attribute, value in ((20, 1), (33, 2), (35, 0x161110), (36, 0xF8F2F0)):
            setting = ctypes.c_uint(value)
            # Older Windows releases may not implement a visual attribute.
            # The native frame remains usable when DWM declines it.
            dwm.DwmSetWindowAttribute(window, attribute, ctypes.byref(setting), ctypes.sizeof(setting))
