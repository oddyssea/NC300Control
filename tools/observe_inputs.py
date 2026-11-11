"""Observe NC300 physical inputs in a bounded test session; no saved edits."""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nc300.device import NC300Device


async def main(args):
    device = NC300Device(args.address)
    events = []
    start = time.monotonic()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stop_file = output.with_suffix(".stop")
    stop_file.unlink(missing_ok=True)
    try:
        await device.connect()
        before = await device.read_snapshot()
        await device.start_input_monitor()
        print("PHYSICAL_MONITOR_READY", flush=True)
        last = None
        until = time.monotonic() + args.duration
        while time.monotonic() < until and not stop_file.exists():
            state = device.physical_input()
            if state and state != last:
                entry = {"time": round(time.monotonic() - start, 3), "state": state}
                events.append(entry)
                output.write_text(json.dumps({"events": events}, indent=2))
                print(json.dumps({"time": entry["time"], "held": state["physical_codes"]}), flush=True)
                last = state
            await asyncio.sleep(0.02)
        await device.stop_input_monitor()
        after = await device.read_snapshot()
        sections = ("sticks", "triggers", "motors", "remappings", "turbo", "macro_ML", "macro_MR")
        retained = all(before["raw"].get(key) == after["raw"].get(key) for key in sections)
        result = {"events": events, "saved_settings_unchanged": retained,
                  "monitor_error": device.input_monitor_error}
        output.write_text(json.dumps(result, indent=2))
        assert retained, "A saved setting changed during input observation"
        print(json.dumps({"finished": True, "events": len(events), "saved_settings_unchanged": retained}), flush=True)
    finally:
        await device.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--output", default="validation/physical-inputs.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(args))
