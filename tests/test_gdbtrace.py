#!/usr/bin/env python3
"""Runs gdbtrace.py against tests/fake_gdbstub.py."""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "28531"
stub = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_gdbstub.py"), PORT],
                        stdout=subprocess.PIPE, text=True)
assert stub.stdout.readline().strip() == "ready"
out = os.path.join(tempfile.mkdtemp(), "trace.json")
r = subprocess.run([sys.executable, os.path.join(ROOT, "gdbtrace.py"), "--port", PORT, "--addr", "0x00010224",
                    "--addr", "0x00010230", "--addr", "0x00020100", "--addr", "0x00030000",
                    "--watch", "0x302518C8", "--seconds", "3", "-o", out], capture_output=True, text=True)
time.sleep(0.3)
left = stub.stdout.readline().strip()
stub.wait(timeout=5)
hits = json.load(open(out))["hits"]


def check(cond, what):
    print(("   ok   " if cond else "   FAIL ") + what)
    if not cond:
        print(r.stdout, r.stderr)
        sys.exit(1)


check(r.returncode == 0 and len(hits) == 3, "three of four breakpoints hit, the dead one reported")
check(all(h["thread"] == "0x1000002" for h in hits), "registers taken from the thread that stopped, not the first one")
check([h["ea"] for h in hits] == ["0x302518C8", "0x302518E4", "0x309901A8"], "effective addresses computed")
check([h["watched"] for h in hits] == [True, False, False], "the instruction touching the watched address is flagged")
check(left == "left planted: []", "all breakpoints removed before disconnecting")
print("GDBTRACE OK")
