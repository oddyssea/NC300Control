"""Exercise the actual GUI's apply/backup/Undo pipeline against the NC300."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nc300.device import NC300Device
from nc300.ui import ControlApp, configure_logging, APP_DATA


def main(address):
    configure_logging()
    root = tk.Tk()
    root.withdraw()
    callback_errors = []
    root.report_callback_exception = lambda *error: callback_errors.append(str(error))
    app = ControlApp(root, auto_connect=False)
    device = NC300Device(address)
    baseline = None
    report = {"passed": False, "checks": []}

    def wait_idle():
        deadline = time.monotonic() + 45
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        if app.busy:
            raise TimeoutError("UI operation did not complete")
        if callback_errors:
            raise AssertionError(callback_errors)

    async def connect():
        await device.connect()
        return await device.read_snapshot()

    try:
        app.device = device
        app._begin("UI validation: reading controller", connect(), app._show_snapshot)
        wait_idle()
        baseline = app.snapshot
        assert baseline and not baseline["errors"], "Could not read live settings"
        assert len(app.mapping_tree.get_children()) == len(baseline["remap_sources"])
        assert float(app.setting_widgets["sticks_0"][0]["to"]) == 89
        assert float(app.setting_widgets["triggers_0"][0]["to"]) == 74
        report["checks"].append("Live snapshot populated widgets and valid deadzone ranges")

        backups_before = set((APP_DATA / "backups").glob("*.json"))
        app.setting_vars["motor_left"].set(51)
        app.setting_vars["motor_right"].set(49)
        app.apply_motor()
        wait_idle()
        assert str(app.status_label["text"]).startswith("Verified:"), app.status_label["text"]
        assert app.snapshot["raw"]["motors"] != baseline["raw"]["motors"]
        assert app.undo_stack
        new_backups = set((APP_DATA / "backups").glob("*.json")) - backups_before
        assert new_backups, "Apply did not save a disk backup"
        saved = json.loads(next(iter(new_backups)).read_text(encoding="utf-8"))
        assert saved["snapshot"]["raw"]["motors"] == baseline["raw"]["motors"]
        report["checks"].append("GUI Apply changed motors, verified hardware, and saved original backup")

        app.undo()
        wait_idle()
        assert app.snapshot["raw"]["motors"] == baseline["raw"]["motors"]
        assert not app.undo_stack
        assert str(app.status_label["text"]).startswith("Verified:"), app.status_label["text"]
        report["checks"].append("GUI Undo restored exact motor record and cleared undo entry")

        app._diagram_select_mapping(1)
        assert app.source_codes[app.source_var.get()] == 1
        app.target_var.set(next(label for label, code in app.target_codes.items() if code == 2))
        app.apply_mapping()
        wait_idle()
        assert app.snapshot["remappings"].get("1") == 2
        assert str(app.status_label["text"]).startswith("Verified:"), app.status_label["text"]
        app.undo()
        wait_idle()
        assert not app.snapshot["remappings"] and not app.undo_stack
        assert str(app.status_label["text"]).startswith("Verified:"), app.status_label["text"]
        report["checks"].append("Controller diagram selection, remapping Apply, and Undo verified against hardware")
        report["passed"] = True
    finally:
        if baseline and device.connected:
            for section in ("motors", "remappings"):
                future = asyncio.run_coroutine_threadsafe(
                    device.apply_raw(section, baseline["raw"][section]), app.worker.loop)
                future.result(timeout=30)
        app.close()
        deadline = time.monotonic() + 15
        while not app.destroyed and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert app.destroyed, "GUI did not close"
        app.worker.thread.join(timeout=4)
        assert not app.worker.thread.is_alive(), "Bluetooth worker did not stop"
        report["checks"].append("GUI closed and Bluetooth worker stopped cleanly")
        Path("validation").mkdir(exist_ok=True)
        Path("validation/ui-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    main(parser.parse_args().address)
