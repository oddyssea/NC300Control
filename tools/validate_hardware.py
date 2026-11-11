"""Read the controller; --exercise also tests reversible edits and restores them."""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nc300.device import NC300Device
from nc300.xinput import get_states


async def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    device = NC300Device(args.address)
    report = {"started": datetime.now(timezone.utc).isoformat(), "tests": []}
    try:
        await device.connect()
        baseline = await device.read_snapshot()
        (output / "before.json").write_text(json.dumps(baseline, indent=2))
        if baseline["errors"]:
            raise RuntimeError(f"Initial reads failed: {baseline['errors']}")
        report["name"], report["firmware"] = baseline["name"], baseline["firmware"]
        report["input_states"] = get_states()
        if args.exercise:
            cases = [
                ("motors", lambda: device.set_motor_strength(51, 49)),
                ("sticks", lambda: device.set_deadzone("sticks", 0, 11)),
                ("triggers", lambda: device.set_deadzone("triggers", 0, 6)),
                ("remappings", lambda: device.set_mapping(1, 2)),
            ]
            for section, mutate in cases:
                entry = {"section": section, "before": baseline["raw"][section]}
                report["tests"].append(entry)
                try:
                    changed = await mutate()
                    entry["changed"] = changed["raw"][section]
                    if entry["changed"] == entry["before"]:
                        raise AssertionError(f"{section} did not change")
                    entry["change_verified"] = True
                    print(f"{section}: changed and read back", flush=True)
                finally:
                    restored = await device.apply_raw(section, entry["before"])
                    actual = restored["raw"][section]
                    expected = entry["before"]
                    if section == "remappings" and not any(bytes.fromhex(expected)):
                        if any(bytes.fromhex(actual)):
                            raise AssertionError("Remappings were not restored")
                    elif actual != expected:
                        raise AssertionError(f"{section} was not restored exactly")
                    entry["restored"] = actual
                    entry["restore_verified"] = True
                    print(f"{section}: restored and read back", flush=True)
        await device.disconnect()
        await device.connect()
        final = await device.read_snapshot()
        (output / "after-reconnect.json").write_text(json.dumps(final, indent=2))
        for section in ("motors", "sticks", "triggers", "remappings"):
            expected, actual = baseline["raw"][section], final["raw"][section]
            if section == "remappings" and not any(bytes.fromhex(expected)):
                assert not any(bytes.fromhex(actual)), "Default remapping did not survive reconnect"
            else:
                assert expected == actual, f"{section} changed after reconnect"
        report["reconnect_verified"] = True
        report["passed"] = True
        print("Reconnect: baseline settings retained; input slots:", len(get_states()), flush=True)
    except Exception as exc:
        report["passed"] = False
        report["error"] = repr(exc)
        logging.exception("Hardware validation failed")
        raise
    finally:
        await device.disconnect()
        (output / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--output", default="validation/hardware")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(run(arguments))
