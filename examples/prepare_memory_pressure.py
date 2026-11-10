"""Build bounded pressure fixtures with the real Memory CAS and context selector."""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.memory import CONTEXT_BYTES, Memory
from oicoach_runtime.store import Store, canonical_json


def prepare(directory, rounds):
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="memory-selector-", dir=directory) as temporary:
        root = Path(temporary)
        config_path = root / "runtime.toml"
        initialize(config_path, str(root / "state"), str(root / "workflows"))
        store = Store(load_config(config_path), clock=lambda: clock)
        try:
            memory = Memory(store)
            keys = [f"note-{index:02d}" for index in range(64)]
            for key in keys:
                memory.put("stress", key, "Pressure note", "x" * 512, task_id="synthetic")
            small = memory.context("stress", "synthetic")
            assert len(small["notes"]) == len(keys) and not small["truncated"]
            # Reserve space for all CAS versions growing to two digits.
            note_bytes = 512 + (CONTEXT_BYTES - len(canonical_json(small).encode()) - 128) // len(keys)
            for key in keys:
                memory.put("stress", key, "Pressure note", "x" * note_bytes, expected_version=1, task_id="synthetic")
            initial = memory.context("stress", "synthetic")
            for turn in range(1, rounds + 1):
                key = keys[(turn - 1) % len(keys)]
                old = memory.show("stress", key, task_id="synthetic")
                memory.put("stress", key, "Pressure note", f"{turn:08d}" + "x" * (note_bytes - 8),
                           expected_version=old["version"], task_id="synthetic")
            final = memory.context("stress", "synthetic")
            for value in (initial, final):
                assert len(value["notes"]) == len(keys) and not value["truncated"]
                assert len(canonical_json(value).encode()) <= CONTEXT_BYTES
            return {"initial": initial, "final": final, "note_content_bytes": note_bytes,
                    "producer": "Memory.put CAS + Memory.context, full metadata/envelope, fixed test clock"}
        finally:
            store.close()


if __name__ == "__main__":
    directory = Path(sys.argv[1]).resolve()
    if not directory.is_relative_to(Path(tempfile.gettempdir())):
        raise ValueError("pressure fixtures require temporary storage")
    print(json.dumps(prepare(directory, int(sys.argv[2])), ensure_ascii=False, separators=(",", ":")))
