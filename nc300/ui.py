"""Local desktop controls for the NC300. Bluetooth owns settings, USB owns input."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Callable, Coroutine

from bleak import BleakScanner

from .device import NC300Device
from .diagram import ControllerDiagram
from .theme import apply_theme, BG, PANEL, FIELD, TEXT, MUTED, ACCENT, ERROR
from .paths import data_directory
from .vendor.protocol import Key
from .xinput import get_states, rumble, stop_all_rumble


APP_DATA = data_directory()
LOG = logging.getLogger("nc300.ui")


def configure_logging() -> None:
    """Keep diagnostics local, without duplicating handlers on repeat launches."""
    logs = APP_DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nc300")
    logger.setLevel(logging.INFO)
    if not any(getattr(handler, "_nc300_file", False) for handler in logger.handlers):
        handler = RotatingFileHandler(logs / "nc300.log", maxBytes=1_000_000,
                                     backupCount=3, encoding="utf-8")
        handler._nc300_file = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)


class AsyncWorker:
    """One BLE event loop; completions are drained by Tk's main thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.completed: queue.Queue[tuple[Callable, Future]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="NC300-Bluetooth", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def submit(self, coro: Coroutine, callback: Callable[[Future], None]) -> Future:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(lambda done: self.completed.put((callback, done)))
        return future

    def stop(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)


def key_label(code: int) -> str:
    if code == 0:
        return "Original button"
    names = {"LB": "L / LB", "RB": "R / RB", "LT": "ZL / LT", "RT": "ZR / RT",
             "SELECT": "− / Back", "START": "+ / Start", "L3": "Left stick click",
             "R3": "Right stick click", "M_LEFT": "Rear ML", "M2": "Rear M2"}
    try:
        name = Key(code).name
        return names.get(name, name.replace('_', ' ').title())
    except ValueError:
        return f"Button {code}"


class ControlApp:
    def __init__(self, root: tk.Tk, *, auto_connect: bool = True) -> None:
        self.root = root
        self.worker = AsyncWorker()
        self.device: NC300Device | None = None
        self.snapshot: dict[str, Any] | None = None
        self.busy = False
        self.active_future: Future | None = None
        self.closing = False
        self.destroyed = False
        self.undo_stack: list[dict[str, str]] = []
        self.action_widgets: list[ttk.Widget] = []
        self.setting_widgets: dict[str, list[ttk.Widget]] = {}
        self.setting_vars: dict[str, tk.DoubleVar] = {}
        self.source_codes: dict[str, int] = {}
        self.target_codes: dict[str, int] = {}
        self.last_slots: list[int] = []
        self.input_slots_reported = False
        self.input_error_logged = False
        self.latest_input_state: dict | None = None
        self.rear_swap_var = tk.BooleanVar(value=False)
        try:
            rear_layout = json.loads((APP_DATA / "rear-layout.json").read_text(encoding="utf-8"))
            self.rear_swap_var.set(rear_layout.get("swapped") is True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            LOG.warning("Could not load rear indicator alignment: %s", exc)
        self.physical_feedback_var = tk.BooleanVar(value=True)
        self.monitor_desired = False
        self.monitor_enabled = False
        self.monitor_transition_pending = False
        self.monitor_device: NC300Device | None = None
        self.monitor_failed_device: NC300Device | None = None
        self.monitor_policy_after: str | None = None
        self._theme()
        self._build()
        for diagram in (self.live_diagram, self.remap_diagram):
            diagram.set_rear_swapped(self.rear_swap_var.get())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self._drain)
        self.root.after(75, self._poll_input)
        self.monitor_policy_after = self.root.after(100, self._poll_monitor_policy)
        self.root.after(100, lambda: LOG.info("UI ready: %sx%s pixels", self.root.winfo_width(), self.root.winfo_height()))
        self._refresh_enabled()
        if auto_connect:
            self.root.after(250, self.scan_connect)

    def _theme(self) -> None:
        self.root.title("NC300 Control")
        self.root.geometry("1180x820")
        self.root.minsize(900, 700)
        apply_theme(self.root)

    def _build(self) -> None:
        shell = ttk.Frame(self.root, padding=(28, 22))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 18))
        brand = ttk.Frame(header)
        brand.pack(side="left")
        ttk.Label(brand, text="NC300 Control", style="Title.TLabel").pack(side="left")
        self.connect_button = ttk.Button(header, text="Connect controller", style="Accent.TButton", command=self.scan_connect)
        self.connect_button.pack(side="right")

        connection = ttk.Frame(shell, style="Card.TFrame", padding=(18, 13))
        connection.pack(fill="x")
        identity = ttk.Frame(connection, style="Card.TFrame")
        identity.pack(side="left", fill="x", expand=True)
        self.connection_label = ttk.Label(identity, text="Controller offline", style="CardHeading.TLabel")
        self.connection_label.pack(anchor="w")
        self.identity_label = ttk.Label(identity, text="Connect to read your settings", style="CardMuted.TLabel")
        self.identity_label.pack(anchor="w", pady=(4, 0))
        tools = ttk.Frame(connection, style="Card.TFrame")
        tools.pack(side="right")
        self.read_button = ttk.Button(tools, text="Refresh", command=self.read_settings)
        self.read_button.pack(side="left")
        self.undo_button = ttk.Button(tools, text="Undo", command=self.undo)
        self.undo_button.pack(side="left", padx=7)
        self.backup_button = ttk.Button(tools, text="Backups", command=self.open_backups)
        self.backup_button.pack(side="left")

        navigation = ttk.Frame(shell)
        navigation.pack(fill="x", pady=(19, 14))
        self.tabs = ttk.Notebook(shell, style="Control.TNotebook")
        self.live_tab = ttk.Frame(self.tabs, padding=0)
        self.remap_tab = ttk.Frame(self.tabs, padding=0)
        self.settings_tab = ttk.Frame(self.tabs, padding=0)
        self.nav_buttons = {}
        for index, (frame, label) in enumerate(((self.live_tab, "Live input"), (self.remap_tab, "Remapping"), (self.settings_tab, "Settings")), 1):
            self.tabs.add(frame, text=label)
            button = ttk.Button(navigation, text=label, style="Nav.TButton", command=lambda f=frame: self.tabs.select(f))
            button.pack(side="left", padx=(0, 6))
            self.nav_buttons[str(frame)] = button
            self.root.bind(f"<Control-Key-{index}>", lambda _event, f=frame: self.tabs.select(f))
        self.physical_feedback_checkbox = ttk.Checkbutton(navigation, text="Physical feedback", variable=self.physical_feedback_var, command=self._feedback_changed)
        self.physical_feedback_checkbox.pack(side="right")
        self.tabs.bind("<<NotebookTabChanged>>", self._page_changed, add="+")
        self._build_live()
        self._build_remapping()
        self._build_settings()

        footer = ttk.Frame(shell)
        footer.pack(side="bottom", fill="x", pady=(14, 0))
        self.status_label = ttk.Label(footer, text="Ready", style="Muted.TLabel", wraplength=930)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(300, event.width)))
        self.detail_label = ttk.Label(footer, text="LOCAL CONNECTION", style="Eyebrow.TLabel")
        self.detail_label.pack(side="right", padx=(15, 0))
        self.tabs.pack(fill="both", expand=True)
        self._page_changed()

    def _page_changed(self, _event: Any = None) -> None:
        selected = self.tabs.select()
        for frame, button in self.nav_buttons.items():
            button.configure(style="Selected.Nav.TButton" if frame == selected else "Nav.TButton")
        self.root.after(100, self._sync_monitor_policy)

    def _build_live(self) -> None:
        top = ttk.Frame(self.live_tab)
        top.pack(fill="x")
        self.slot_var = tk.StringVar()
        self.slot_combo = ttk.Combobox(top, textvariable=self.slot_var, state="readonly", width=15)
        self.slot_combo.pack(side="right")
        self.live_view_var = tk.StringVar(value="Front")
        view = ttk.Combobox(top, textvariable=self.live_view_var, values=("Front", "Rear"), state="readonly", width=8)
        view.pack(side="right", padx=9)
        view.bind("<<ComboboxSelected>>", lambda _event: self.live_diagram.set_view(self.live_view_var.get().lower()))
        ttk.Checkbutton(top, text="Swap rear indicators", variable=self.rear_swap_var,
                        command=self._swap_rear_indicators).pack(side="right", padx=4)
        self.live_status = ttk.Label(self.live_tab, text="Looking for a USB / XInput controller...", style="Muted.TLabel")
        self.live_status.pack(anchor="w", pady=(5, 6))
        self.live_note = ttk.Label(self.live_tab,
            text="Highlights show Xbox output after remapping. Dim system and rear controls have no separate Windows input signal.",
            style="Muted.TLabel", wraplength=970)
        self.live_note.pack(side="bottom", fill="x", pady=(5, 0))
        self.live_control_label = ttk.Label(self.live_tab,
            text="Press any control to see it respond. Click to inspect.", style="Muted.TLabel", wraplength=970)
        self.live_control_label.pack(side="bottom", fill="x", pady=(4, 0))
        body = ttk.Frame(self.live_tab)
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, style="Card.TFrame", width=252)
        sidebar.pack(side="right", fill="y", padx=(12, 0))
        sidebar.pack_propagate(False)
        # The readout scrolls for long button chords and small window sizes.
        viewport = tk.Canvas(sidebar, bg=PANEL, highlightthickness=0, width=236)
        scroll = ttk.Scrollbar(sidebar, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        readout = ttk.Frame(viewport, style="Card.TFrame", padding=12)
        window = viewport.create_window((0, 0), window=readout, anchor="nw")
        readout.bind("<Configure>", lambda _event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(window, width=event.width))
        self.live_diagram = ControllerDiagram(body, height=300, bg=PANEL, on_select=self._inspect_live_control)
        self.live_diagram.pack(side="left", fill="both", expand=True)
        self.stick_canvas = self.live_diagram  # Kept for existing automation.
        ttk.Label(readout, text="GAME OUTPUT", style="CardHeading.TLabel").pack(anchor="w", pady=(0, 8))
        self.buttons_label = ttk.Label(readout, text="Buttons: none", style="Card.TLabel", wraplength=199)
        self.buttons_label.pack(anchor="w", pady=(0, 9))
        self.stick_values_label = ttk.Label(readout, text="Left stick +0.00, +0.00\nRight stick +0.00, +0.00", style="CardMuted.TLabel")
        self.stick_values_label.pack(anchor="w", pady=(0, 9))
        self.trigger_bars: list[ttk.Progressbar] = []
        self.trigger_labels: list[ttk.Label] = []
        for name in ("Left trigger", "Right trigger"):
            label = ttk.Label(readout, text=f"{name} · 0%", style="CardMuted.TLabel")
            label.pack(anchor="w", pady=(0, 3))
            bar = ttk.Progressbar(readout, maximum=1)
            bar.pack(fill="x", pady=(0, 10))
            self.trigger_labels.append(label)
            self.trigger_bars.append(bar)
        self.rumble_button = ttk.Button(readout, text="Test vibration · ½ sec", command=self.test_rumble)
        self.rumble_button.pack(fill="x", pady=(3, 5))
        ttk.Label(readout, text="Selected Windows slot, 30% strength.", style="CardMuted.TLabel", wraplength=199).pack(anchor="w")
        self.live_tab.bind("<Configure>", lambda event: [label.configure(wraplength=max(300, event.width - 4))
            for label in (self.live_note, self.live_control_label)], add="+")

    def _build_remapping(self) -> None:
        heading = ttk.Frame(self.remap_tab)
        heading.pack(fill="x")
        self.remap_view_var = tk.StringVar(value="Front")
        view = ttk.Combobox(heading, textvariable=self.remap_view_var, values=("Front", "Rear"), state="readonly", width=8)
        view.pack(side="right")
        view.bind("<<ComboboxSelected>>", lambda _event: self.remap_diagram.set_view(self.remap_view_var.get().lower()))
        self.remap_input_note = ttk.Label(self.remap_tab,
                  text="Each Apply saves to the controller. Connect Bluetooth to monitor physical buttons before remapping.",
                  style="Muted.TLabel", wraplength=980)
        self.remap_input_note.pack(anchor="w", pady=(5, 8))
        work = ttk.Frame(self.remap_tab)
        work.pack(fill="both", expand=True)
        self.remap_diagram = ControllerDiagram(work, width=600, height=285, bg=PANEL,
                                              on_select=self._diagram_select_mapping)
        self.remap_diagram.pack(side="left", fill="both", expand=True)
        sidebar = ttk.Frame(work, style="Card.TFrame", width=252)
        sidebar.pack(side="right", fill="y", padx=(12, 0))
        sidebar.pack_propagate(False)
        viewport = tk.Canvas(sidebar, bg=PANEL, highlightthickness=0, width=236)
        scroll = ttk.Scrollbar(sidebar, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(viewport, style="Card.TFrame", padding=(12, 10))
        window = viewport.create_window((0, 0), window=row, anchor="nw")
        row.bind("<Configure>", lambda _event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(window, width=event.width))
        self.mapping_selection_label = ttk.Label(row, text="Connect to choose a button", style="CardHeading.TLabel", wraplength=210)
        self.mapping_current_label = ttk.Label(row, text="Current action: —", style="CardMuted.TLabel", wraplength=210)
        self.source_var = tk.StringVar()
        self.target_var = tk.StringVar()
        for label, variable, attr in [("Button", self.source_var, "source_combo"),
                                       ("Assign action", self.target_var, "target_combo")]:
            cell = ttk.Frame(row, style="Card.TFrame")
            cell.pack(fill="x", pady=(0, 5))
            ttk.Label(cell, text=label, style="CardMuted.TLabel").pack(anchor="w", pady=(0, 3))
            combo = ttk.Combobox(cell, textvariable=variable, state="readonly", width=24)
            combo.pack(fill="x")
            setattr(self, attr, combo)
        cell.pack_forget()
        self.mapping_current_label.pack(anchor="w", pady=(0, 5))
        cell.pack(fill="x", pady=(0, 5))
        self.remap_apply = ttk.Button(row, text="Apply mapping", style="Accent.TButton", command=self.apply_mapping)
        self.remap_apply.pack(fill="x", pady=(2, 0))
        self.mapping_help_label = ttk.Label(row, text="Select a lit control on the diagram. Dim controls cannot be remapped by this firmware.",
                                           style="CardMuted.TLabel", wraplength=210)
        self.mapping_help_label.pack(anchor="w")
        viewport.bind("<Configure>", self._resize_mapping_help, add="+")
        # Reserve the side panel before allocating the flexible canvas width.
        self.remap_diagram.pack_forget()
        self.remap_diagram.pack(side="left", fill="both", expand=True)
        self.action_widgets.append(self.remap_apply)
        self.source_combo.bind("<<ComboboxSelected>>", self._select_mapping)
        self.mapping_table_visible = tk.BooleanVar(value=False)
        table_toggle = ttk.Checkbutton(self.remap_tab, text="Show all mappings", variable=self.mapping_table_visible,
                                      command=self._toggle_mapping_table)
        table_toggle.pack(anchor="w", pady=(5, 0))
        self.mapping_table_frame = ttk.Frame(self.remap_tab)
        table = self.mapping_table_frame
        self.mapping_tree = ttk.Treeview(table, columns=("source", "target"), show="headings", height=3)
        self.mapping_tree.heading("source", text="Pressed button")
        self.mapping_tree.heading("target", text="Current action")
        self.mapping_tree.column("source", width=320)
        self.mapping_tree.column("target", width=450)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.mapping_tree.yview)
        self.mapping_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.mapping_tree.pack(side="left", fill="both", expand=True)
        self.mapping_tree.bind("<<TreeviewSelect>>", self._table_select_mapping)
        self.remap_note = ttk.Label(self.remap_tab, text="Connect to read the supported remapping buttons.",
                                     style="Muted.TLabel", wraplength=900)
        self.remap_note.pack(anchor="w", pady=(5, 0))
        # Bottom controls remain visible when the diagram's height shrinks.
        work.pack_forget()
        table_toggle.pack_forget()
        self.remap_note.pack_forget()
        self.remap_note.pack(side="bottom", fill="x", pady=(4, 0))
        table_toggle.pack(side="bottom", anchor="w", pady=(4, 0))
        work.pack(fill="both", expand=True)
        self.remap_tab.bind("<Configure>", lambda event: [label.configure(wraplength=max(300, event.width - 4))
            for label in (self.remap_input_note, self.remap_note)], add="+")

    def _swap_rear_indicators(self) -> None:
        for diagram in (self.live_diagram, self.remap_diagram):
            diagram.set_rear_swapped(self.rear_swap_var.get())
        try:
            APP_DATA.mkdir(parents=True, exist_ok=True)
            (APP_DATA / "rear-layout.json").write_text(
                json.dumps({"swapped": self.rear_swap_var.get()}) + "\n", encoding="utf-8")
        except OSError as exc:
            self._status(f"Rear indicators aligned for this session; preference could not be saved: {exc}", error=True)
        else:
            self._status("Rear indicator positions aligned. Controller assignments are unchanged.")

    def _toggle_mapping_table(self) -> None:
        if self.mapping_table_visible.get():
            self.mapping_table_frame.pack(side="bottom", fill="x", pady=(5, 0), before=self.remap_note)
        else:
            self.mapping_table_frame.pack_forget()

    def _resize_mapping_help(self, event: Any) -> None:
        if event.height < 330:
            self.mapping_help_label.pack_forget()
        elif not self.mapping_help_label.winfo_manager():
            self.mapping_help_label.pack(anchor="w")

    def _table_select_mapping(self, _event: Any = None) -> None:
        selected = self.mapping_tree.selection()
        if selected:
            label = self.mapping_tree.item(selected[0], "values")[0]
            if label in self.source_codes:
                self.source_var.set(label)
                self._select_mapping()

    def _inspect_live_control(self, code: int) -> None:
        self.live_diagram.set_selected(code)
        info = self.live_diagram.control_info(code)
        self.live_control_label.configure(text=f"{info['label']}: " + (
            "Input is available. Press it to see the highlight." if info.get("reported")
            else "No separate signal is available for this control; its physical press cannot be shown."))

    def _diagram_select_mapping(self, code: int) -> None:
        label = next((name for name, value in self.source_codes.items() if value == code), None)
        if label is None:
            info = self.remap_diagram.control_info(code)
            self.remap_diagram.set_selected(code)
            self.mapping_selection_label.configure(text=info["label"])
            self.mapping_current_label.configure(text="Remapping unavailable")
            self.mapping_help_label.configure(text=("Connect and read settings to discover supported buttons." if not self.snapshot
                else "This controller does not offer this control as a remapping source. Choose a lit button."))
            self._status(self.mapping_help_label.cget("text"))
            self.source_var.set("")
            self.target_var.set("")
            self._refresh_enabled()
            return
        self.source_var.set(label)
        self._select_mapping()
        self._status(f"Selected {label}. Choose an action, then Apply mapping to save it.")

    def _build_settings(self) -> None:
        viewport = tk.Canvas(self.settings_tab, background=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(self.settings_tab, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(viewport, padding=(0, 0, 12, 5))
        window = viewport.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(window, width=event.width))

        def scroll_settings(event: Any) -> None:
            if self.tabs.select() == str(self.settings_tab):
                viewport.yview_scroll(-int(event.delta / 120), "units")

        self.root.bind("<MouseWheel>", scroll_settings, add="+")
        ttk.Label(body, text="Adjust a value, then Apply. An exact backup is saved before every change.",
                  style="Muted.TLabel").pack(anchor="w", pady=(5, 10))
        for section, side, title in [("sticks", 0, "Left stick deadzone"), ("sticks", 1, "Right stick deadzone"),
                                      ("triggers", 0, "Left trigger deadzone"), ("triggers", 1, "Right trigger deadzone")]:
            name = f"{section}_{side}"
            row = ttk.Frame(body, style="Card.TFrame", padding=(18, 14))
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=title, width=25, style="Card.TLabel").pack(side="left")
            var = tk.DoubleVar(value=0)
            self.setting_vars[name] = var
            value = ttk.Label(row, text="—", width=7, style="Card.TLabel", anchor="e")
            value.pack(side="right", padx=9)
            apply = ttk.Button(row, text="Apply", command=lambda s=section, i=side: self.apply_deadzone(s, i))
            apply.pack(side="right")
            scale = ttk.Scale(row, from_=0, to=100, variable=var,
                              command=lambda val, out=value: out.configure(text=f"{float(val):.0f}%"))
            scale.pack(side="left", fill="x", expand=True, padx=10)
            self.setting_widgets[name] = [scale, apply]
            self.action_widgets.append(apply)
            setattr(self, f"value_{name}", value)
        motor = ttk.Frame(body, style="Card.TFrame", padding=(18, 16))
        motor.pack(fill="x", pady=(10, 3))
        ttk.Label(motor, text="Vibration strength", style="CardHeading.TLabel").pack(anchor="w", pady=(0, 5))
        self.motor_widgets: list[ttk.Widget] = []
        for side in ("left", "right"):
            row = ttk.Frame(motor, style="Card.TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"{side.title()} motor", width=25, style="Card.TLabel").pack(side="left")
            var = tk.DoubleVar(value=0)
            self.setting_vars[f"motor_{side}"] = var
            value = ttk.Label(row, text="—", width=7, anchor="e", style="Card.TLabel")
            value.pack(side="right", padx=(9, 82))
            scale = ttk.Scale(row, from_=0, to=100, variable=var,
                              command=lambda val, out=value: out.configure(text=f"{float(val):.0f}%"))
            scale.pack(side="left", fill="x", expand=True, padx=10)
            self.motor_widgets.append(scale)
            setattr(self, f"value_motor_{side}", value)
        self.motor_apply = ttk.Button(motor, text="Apply vibration strength", command=self.apply_motor)
        self.motor_apply.pack(anchor="e", pady=(6, 0))
        self.motor_widgets.append(self.motor_apply)
        self.action_widgets.append(self.motor_apply)





    def _status(self, message: str, *, error: bool = False) -> None:
        self.status_label.configure(text=message, foreground=ERROR if error else MUTED)
        (LOG.error if error else LOG.info)(message)

    def _refresh_enabled(self) -> None:
        connected = self.device is not None and self.snapshot is not None
        readable = connected and not self.busy and not self.closing
        available = readable and bool(self.snapshot and self.snapshot.get("writable"))
        self.connect_button.configure(state="disabled" if self.busy or self.closing else "normal")
        self.read_button.configure(state="normal" if readable else "disabled")
        self.undo_button.configure(state="normal" if available and self.undo_stack else "disabled")
        raw = self.snapshot.get("raw", {}) if self.snapshot else {}
        caps = (self.snapshot.get("capabilities") or {}) if self.snapshot else {}
        errors = self.snapshot.get("errors", {}) if self.snapshot else {}
        for name, widgets in self.setting_widgets.items():
            section, side = name.rsplit("_", 1)
            # Capability bits follow the controller: bit 1 is left, bit 0 right.
            supported = bool(int(caps.get(section, 0)) & (2 if side == "0" else 1))
            enabled = available and supported and section not in errors and self._raw_length(raw.get(section)) == 14
            for widget in widgets:
                widget.configure(state="normal" if enabled else "disabled")
        enabled = available and caps.get("motors", 0) & 3 == 3 and "motors" not in errors and self._raw_length(raw.get("motors")) == 8
        for widget in self.motor_widgets:
            widget.configure(state="normal" if enabled else "disabled")
        remap = available and bool(self.source_codes) and bool(self.target_codes) and "remappings" in raw and "remappings" not in errors
        chosen = self.source_var.get() in self.source_codes and self.target_var.get() in self.target_codes
        self.remap_apply.configure(state="normal" if remap and chosen else "disabled")
        for combo in (self.source_combo, self.target_combo):
            combo.configure(state="readonly" if remap else "disabled")
        self.rumble_button.configure(state="normal" if self.last_slots and not self.busy and not self.closing else "disabled")

    @staticmethod
    def _raw_length(value: Any) -> int:
        try:
            return len(bytes.fromhex(value)) if isinstance(value, str) else -1
        except ValueError:
            return -1

    def _begin(self, text: str, operation: Coroutine, success: Callable[[Any], None],
               failure: Callable[[Exception], None] | None = None) -> None:
        if self.busy or self.closing:
            operation.close()
            return
        self.busy = True
        self._status(text)
        self._refresh_enabled()

        def done(future: Future) -> None:
            self.busy = False
            try:
                result = future.result()
                success(result)
            except Exception as exc:
                LOG.exception("Operation failed")
                if failure:
                    failure(exc)
                else:
                    self._status(str(exc) or type(exc).__name__, error=True)
            self._refresh_enabled()

        self.active_future = self.worker.submit(operation, done)

    def _drain(self) -> None:
        if self.destroyed:
            return
        try:
            while True:
                callback, future = self.worker.completed.get_nowait()
                callback(future)
        except queue.Empty:
            pass
        if not self.destroyed:
            self.root.after(40, self._drain)

    def scan_connect(self) -> None:
        async def work() -> dict[str, Any]:
            if self.device is not None:
                await self.device.disconnect()
                self.device = None
            found = await BleakScanner.discover(timeout=8, return_adv=True)
            candidates = []
            for candidate, advertisement in found.values():
                name = advertisement.local_name or candidate.name or ""
                if name.upper().startswith("NC300"):
                    candidates.append(candidate)
            if not candidates:
                raise RuntimeError("No NC300 settings signal found. Keep Bluetooth on and the controller powered; try Scan & connect again.")
            if len(candidates) > 1:
                raise RuntimeError("Several NC300 controllers are nearby. Power off the others, then scan again.")
            device = NC300Device(candidates[0].address)
            try:
                await device.connect()
                snapshot = await device.read_snapshot()
            except Exception:
                await device.disconnect()
                raise
            self.device = device
            return snapshot

        self.snapshot = None
        self.connection_label.configure(text="Bluetooth settings: scanning…")

        def success(snapshot: dict) -> None:
            self._show_snapshot(snapshot)
            if snapshot.get("writable"):
                self._status("Connected. Settings read from the controller; no changes made.")
            else:
                self._status("Connected for reading. Settings writes are unavailable because the NC300 firmware identity could not be verified.", error=True)

        def failure(exc: Exception) -> None:
            self.connection_label.configure(text="Controller offline", foreground=MUTED)
            self.identity_label.configure(text="USB input is still available in Live input")
            self._status(str(exc), error=True)

        self._begin("Scanning for the NC300 Bluetooth settings connection…", work(), success, failure)

    def _feedback_changed(self) -> None:
        self.monitor_failed_device = None
        self._sync_monitor_policy()

    def _feedback_wanted(self) -> bool:
        """The firmware's button test mode is scoped to active app use."""
        try:
            focused = self.root.focus_get() is not None
        except KeyError:
            # ttk creates its dropdown list in Tcl, so it may have no Python
            # widget wrapper even though it belongs to this app's window.
            focused = bool(self.root.tk.call("focus", "-displayof", self.root._w))
        return bool(not self.closing and not self.destroyed and self.device and self.snapshot
                    and self.snapshot.get("writable")
                    and self.physical_feedback_var.get()
                    and self.root.state() not in ("iconic", "withdrawn")
                    and focused
                    and self.tabs.select() in (str(self.live_tab), str(self.remap_tab)))

    def _poll_monitor_policy(self) -> None:
        self.monitor_policy_after = None
        if self.closing or self.destroyed:
            return
        self._sync_monitor_policy()
        self.monitor_policy_after = self.root.after(100, self._poll_monitor_policy)

    def _sync_monitor_policy(self) -> None:
        if self.destroyed:
            return
        self.monitor_desired = self._feedback_wanted()
        if self.monitor_transition_pending:
            return
        if self.monitor_device is not self.device:
            # Scan/disconnect owns cleanup of the previous device.
            self.monitor_device = self.device
            self.monitor_enabled = False
            self.monitor_failed_device = None
        if not self.monitor_desired:
            self.monitor_failed_device = None
        device = self.device
        if device is None or self.monitor_desired == self.monitor_enabled:
            return
        if self.monitor_desired and self.monitor_failed_device is device:
            return
        desired = self.monitor_desired
        self.monitor_transition_pending = True

        async def transition() -> bool:
            if desired:
                await device.start_input_monitor()
                # Focus/tab changes may arrive while Bluetooth is starting.
                if self.monitor_desired and not self.closing and self.device is device:
                    return True
            await device.stop_input_monitor()
            return False

        def done(future: Future) -> None:
            self.monitor_transition_pending = False
            try:
                enabled = future.result()
            except Exception as exc:
                self.monitor_enabled = False
                self.monitor_failed_device = device
                LOG.exception("Physical button feedback transition failed")
                self._status(f"Physical button feedback unavailable: {exc}. Windows output remains available.", error=True)
            else:
                self.monitor_enabled = bool(enabled) if self.device is device else False
                LOG.info("Physical button feedback %s (active window policy)", "enabled" if self.monitor_enabled else "paused")
            if not self.closing:
                self._sync_monitor_policy()

        self.worker.submit(transition(), done)

    def read_settings(self) -> None:
        async def work() -> dict:
            if self.device is None:
                raise RuntimeError("Connect to the controller first.")
            snapshot = await self.device.read_snapshot()
            return snapshot

        def success(snapshot: dict) -> None:
            self._show_snapshot(snapshot)
            errors = snapshot.get("errors", {})
            self._status("Settings read. " + ("Some sections were unavailable; see details below." if errors else "No changes made."),
                         error=bool(errors))

        self._begin("Reading settings from the controller…", work(), success)

    def _show_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        mode = "connected" if snapshot.get("writable") else "connected · read only"
        self.connection_label.configure(text=f"{snapshot.get('name') or 'NC300'} - {mode}", foreground=ACCENT)
        self.connect_button.configure(text="Reconnect")
        firmware = snapshot.get("firmware", "unknown")
        if isinstance(firmware, (dict, list)):
            firmware = json.dumps(firmware, ensure_ascii=False)
        self.identity_label.configure(text=f"{snapshot.get('address', '')}   ·   Firmware {firmware}")
        raw = snapshot.get("raw", {})
        for section, scale in (("sticks", 100), ("triggers", 200)):
            if self._raw_length(raw.get(section)) == 14:
                data = bytes.fromhex(raw[section])
                for side in (0, 1):
                    value = data[side * 7] / scale * 100
                    name = f"{section}_{side}"
                    max_percent = max(0, (scale - data[side * 7 + 1] - 1) * 100 // scale)
                    self.setting_widgets[name][0].configure(to=max_percent)
                    self.setting_vars[name].set(value)
                    getattr(self, f"value_{name}").configure(text=f"{value:.0f}%")
        if self._raw_length(raw.get("motors")) == 8:
            data = bytes.fromhex(raw["motors"])
            for index, side in enumerate(("left", "right")):
                value = data[index] / 255 * 100
                self.setting_vars[f"motor_{side}"].set(value)
                getattr(self, f"value_motor_{side}").configure(text=f"{value:.0f}%")
        self.source_codes = {key_label(int(code)): int(code) for code in snapshot.get("remap_sources", [])}
        self.target_codes = {key_label(int(code)): int(code) for code in snapshot.get("remap_targets", [])}
        if self.source_codes:
            self.target_codes = {key_label(0): 0, **self.target_codes}
        self.source_combo.configure(values=list(self.source_codes))
        self.target_combo.configure(values=list(self.target_codes))
        self.remap_diagram.set_available(self.source_codes.values())
        if self.source_var.get() not in self.source_codes:
            self.source_var.set(next(iter(self.source_codes), ""))
        self.mapping_tree.delete(*self.mapping_tree.get_children())
        mappings = snapshot.get("remappings", {})
        self.remap_diagram.set_mappings(mappings)
        for label, source in self.source_codes.items():
            target = int(mappings.get(str(source), 0))
            self.mapping_tree.insert("", "end", values=(label, key_label(target)))
        self._select_mapping()
        errors = snapshot.get("errors", {})
        if errors:
            self.remap_note.configure(text="Read warnings: " + "; ".join(f"{key}: {value}" for key, value in errors.items()))
        else:
            self.remap_note.configure(text="Original button restores that button’s default action. Undo restores the exact previous settings.")
        self._refresh_enabled()

    def _select_mapping(self, _event: Any = None) -> None:
        source = self.source_codes.get(self.source_var.get())
        mappings = self.snapshot.get("remappings", {}) if self.snapshot else {}
        target = int(mappings.get(str(source), 0))
        label = key_label(target)
        self.target_var.set(label if label in self.target_codes else next(iter(self.target_codes), ""))
        self.remap_diagram.set_selected(source)
        self.mapping_selection_label.configure(text=self.source_var.get() or "Choose a supported button")
        action = key_label(source) if target == 0 and source is not None else key_label(target)
        self.mapping_current_label.configure(text=f"Current action: {action}")
        self.mapping_help_label.configure(text="Choose an action, then Apply mapping. Original button restores this button’s default action.")
        self._refresh_enabled()

    @staticmethod
    def _save_backup(snapshot: dict, description: str) -> Path:
        directory = APP_DATA / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat()
        path = directory / f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"
        payload = {"saved_at": timestamp, "description": description, "snapshot": snapshot}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        LOG.info("Saved pre-change backup %s", path)
        return path

    def _mutate(self, section: str, description: str, operation: Callable[[NC300Device], Coroutine]) -> None:
        recovery: dict[str, str] = {}

        async def work() -> dict:
            if self.device is None:
                raise RuntimeError("Connect to the controller first.")
            before = await self.device.read_snapshot()
            raw = before.get("raw", {})
            if section not in raw or not isinstance(raw[section], str):
                raise RuntimeError(f"Cannot back up {section}; no setting was changed. Read settings and retry.")
            backup = self._save_backup(before, description)
            recovery.update(section=section, raw=raw[section], backup=str(backup), description=description,
                            address=before.get("address", ""))
            await operation(self.device)
            return await self.device.read_snapshot()

        def success(snapshot: dict) -> None:
            self.undo_stack.append(recovery.copy())
            self._show_snapshot(snapshot)
            self._status(f"Verified: {description}. Backup saved; Undo is available.")

        def failure(exc: Exception) -> None:
            if recovery:
                self.undo_stack.append(recovery.copy())
                self._status(f"{exc} Backup saved; use Undo to restore the previous {section} settings.", error=True)
            else:
                self._status(str(exc), error=True)

        self._begin(f"Saving a backup and applying {description.lower()}…", work(), success, failure)

    def apply_deadzone(self, section: str, side: int) -> None:
        value = round(self.setting_vars[f"{section}_{side}"].get())
        description = f"{'Left' if side == 0 else 'Right'} {'stick' if section == 'sticks' else 'trigger'} deadzone set to {value}%"
        self._mutate(section, description,
                     lambda device: device.set_deadzone(section=section, side=side, inner_percent=value))

    def apply_motor(self) -> None:
        left = round(self.setting_vars["motor_left"].get())
        right = round(self.setting_vars["motor_right"].get())
        self._mutate("motors", f"Vibration strength set to {left}% / {right}%",
                     lambda device: device.set_motor_strength(left, right))

    def apply_mapping(self) -> None:
        source = self.source_codes.get(self.source_var.get())
        target = self.target_codes.get(self.target_var.get())
        if source is None or target is None:
            self._status("Choose a supported source and target button.", error=True)
            return
        self._mutate("remappings", f"{key_label(source)} → {key_label(target)}",
                     lambda device: device.set_mapping(source, target))

    def undo(self) -> None:
        if not self.undo_stack:
            return
        restore = self.undo_stack[-1]

        async def work() -> dict:
            if self.device is None:
                raise RuntimeError("Connect to the controller first.")
            current = await self.device.read_snapshot()
            if current.get("address") != restore["address"]:
                raise RuntimeError("This backup belongs to a different controller. Reconnect the original controller to undo.")
            self._save_backup(current, "Before undo: " + restore["description"])
            await self.device.apply_raw(restore["section"], restore["raw"])
            snapshot = await self.device.read_snapshot()
            expected = bytes.fromhex(restore["raw"])
            actual_hex = snapshot.get("raw", {}).get(restore["section"])
            if actual_hex is None:
                raise RuntimeError("Undo could not read back the restored settings. The original backup is retained.")
            actual = bytes.fromhex(actual_hex)
            equivalent_defaults = (
                restore["section"] == "remappings" and not any(expected) and not any(actual)
                and len(expected) in (0, len(snapshot.get("remap_sources", [])))
                and len(actual) in (0, len(snapshot.get("remap_sources", []))))
            if actual != expected and not equivalent_defaults:
                raise RuntimeError("Undo readback did not match the backup. The original backup is retained.")
            return snapshot

        def success(snapshot: dict) -> None:
            self.undo_stack.pop()
            self._show_snapshot(snapshot)
            self._status(f"Verified: previous {restore['section']} settings restored.")

        self._begin(f"Restoring previous {restore['section']} settings…", work(), success)

    def open_backups(self) -> None:
        directory = APP_DATA / "backups"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            self._status(f"Cannot open the backups folder: {exc}", error=True)

    def _poll_input(self) -> None:
        if self.closing or self.destroyed:
            return
        try:
            states = get_states()
            slots = [state["slot"] for state in states]
            if slots != self.last_slots or not self.input_slots_reported:
                LOG.info("Windows XInput connected slots: %s", [slot + 1 for slot in slots])
                self.input_slots_reported = True
                self.last_slots = slots
                self.slot_combo.configure(values=[f"Slot {slot + 1}" for slot in slots])
                if self.slot_var.get() not in self.slot_combo["values"]:
                    self.slot_var.set(f"Slot {slots[0] + 1}" if slots else "")
                self._refresh_enabled()
            selected = self._selected_slot()
            state = next((state for state in states if state["slot"] == selected), None)
            physical_getter = getattr(self.device, "physical_input", None)
            physical = physical_getter() if callable(physical_getter) else None
            if physical:
                self.live_note.configure(text="Physical input before remapping. Dotted controls do not report a signal. Bumpers may vibrate during testing.")
                self.remap_input_note.configure(text="Physical input before remapping. Select a control to change its assignment.")
            else:
                error = getattr(self.device, "input_monitor_error", None)
                prefix = "Physical Bluetooth input unavailable. " if error else ""
                self.live_note.configure(text=prefix + "Windows input after remapping. Physical feedback is available while this window is active.")
                self.remap_input_note.configure(text="Windows input after remapping. Select a control to change its assignment.")
            if state:
                source = "physical Bluetooth input live" if physical else "Xbox output live"
                self.live_status.configure(text=f"Windows slot {selected + 1} connected • {source}")
                self.buttons_label.configure(text="Buttons: " + ("  ·  ".join(state["buttons"]) or "none"))
            else:
                self.live_status.configure(text=("Physical Bluetooth input live • no Windows Xbox output detected" if physical
                    else "No Windows XInput controller detected. Keep the controller connected by USB."))
                self.buttons_label.configure(text="Windows Xbox output: disconnected")
            self._draw_sticks(state, diagram_state=physical)
            for index, (key, name) in enumerate((("left_trigger", "Left trigger"), ("right_trigger", "Right trigger"))):
                value = state.get(key, 0) if state else 0
                self.trigger_bars[index]["value"] = value
                self.trigger_labels[index].configure(text=f"{name} · {value * 100:.0f}%")
        except Exception as exc:
            if not self.input_error_logged:
                LOG.exception("XInput polling failed")
                self.input_error_logged = True
            self.live_status.configure(text=f"Windows input unavailable: {exc}")
        self.root.after(40, self._poll_input)

    def _selected_slot(self) -> int | None:
        try:
            return int(self.slot_var.get().split()[-1]) - 1
        except (ValueError, IndexError):
            return None

    def _draw_sticks(self, state: dict | None, *, diagram_state: dict | None = None) -> None:
        self.latest_input_state = state
        self.latest_diagram_state = diagram_state or state
        self.live_diagram.set_state(self.latest_diagram_state)
        self.remap_diagram.set_state(self.latest_diagram_state)
        left = state.get("left_stick", (0, 0)) if state else (0, 0)
        right = state.get("right_stick", (0, 0)) if state else (0, 0)
        self.stick_values_label.configure(text=f"Left stick {left[0]:+.2f}, {left[1]:+.2f}\nRight stick {right[0]:+.2f}, {right[1]:+.2f}")

    def test_rumble(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            self._status("Connect a Windows XInput controller first.", error=True)
            return

        async def work() -> None:
            await asyncio.to_thread(rumble, slot, 30, 30, 0.5)

        self._begin("Testing vibration for half a second…", work(),
                    lambda _: self._status("Vibration command sent to Windows. Check the controller for physical feedback."))

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.monitor_policy_after is not None:
            self.root.after_cancel(self.monitor_policy_after)
            self.monitor_policy_after = None
        self._status("Closing the Bluetooth connection…")
        self._refresh_enabled()
        try:
            stop_all_rumble()
        except Exception:
            LOG.exception("Could not stop vibration during shutdown")

        async def cleanup() -> None:
            if self.active_future is not None and not self.active_future.done():
                try:
                    await asyncio.wrap_future(self.active_future)
                except Exception:
                    LOG.exception("Active operation failed during shutdown")
            if self.device is not None:
                await self.device.disconnect()
                self.device = None

        def done(future: Future) -> None:
            try:
                future.result()
            except Exception:
                LOG.exception("Bluetooth disconnect during shutdown failed")
            self._destroy()

        self.worker.submit(cleanup(), done)
        self.root.after(10000, self._destroy)

    def _destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        # Cancel every pending Tk callback, including debounce/idle callbacks,
        # before deleting their registered Tcl commands with the window.
        for callback_id in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback_id)
        self.monitor_policy_after = None
        self.worker.stop()
        self.worker.thread.join(timeout=2)
        if self.worker.thread.is_alive():
            LOG.warning("Bluetooth worker did not finish within the window shutdown deadline")
        self.active_future = None
        while not self.worker.completed.empty():
            self.worker.completed.get_nowait()
        # These callbacks otherwise form a widget -> app -> widget cycle whose
        # Tk variables could later be collected on the Bluetooth worker thread.
        for diagram in (self.live_diagram, self.remap_diagram):
            diagram.on_select = None
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local NC300 controller settings and input monitor")
    parser.add_argument("--smoke-test", action="store_true", help="Construct and close the UI without Bluetooth I/O")
    parser.add_argument("--auto-connect", action=argparse.BooleanOptionalAction, default=True,
                        help="Scan for the NC300 settings connection on launch")
    args = parser.parse_args(argv)
    root = tk.Tk()
    root.withdraw()
    try:
        configure_logging()
    except OSError as exc:
        messagebox.showerror("NC300 Control", f"Cannot write application data to:\n{APP_DATA}\n\n"
                             "Move NC300 Control to a writable folder and try again.\n\n"
                             f"{exc}", parent=root)
        root.destroy()
        return 1
    try:
        app = ControlApp(root, auto_connect=args.auto_connect and not args.smoke_test)
        root.deiconify()
        if args.smoke_test:
            root.after(2000, app.close)
        root.mainloop()
    except Exception:
        LOG.exception("Application failed")
        root.destroy()
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
