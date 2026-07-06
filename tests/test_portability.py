"""The core modules must import on any OS without the Linux-only `dbus_fast`.

Pairing is platform-specific (see `pairing.py`); `distech_ble`, `pairing` and `store`
must never hard-import a Linux dependency at module load, so the app runs natively on
Windows and macOS. This is checked in a fresh subprocess with `dbus_fast` blocked.
"""
import os
import subprocess
import sys

_SNIPPET = """
import sys
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "dbus_fast" or name.startswith("dbus_fast."):
            raise ImportError("dbus_fast blocked (simulated non-Linux install)")
        return None
sys.meta_path.insert(0, _Block())
import distech_ble, pairing, store            # must all import fine
assert pairing.get_pairing_backend() is not None
print("ok")
"""


def test_imports_without_dbus_fast():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
