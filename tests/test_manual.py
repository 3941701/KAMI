#!/usr/bin/env python3
"""Checks camfind's manual mode: a robot reads the on-screen instructions, moves the
simulator's sticks accordingly and presses Enter, like a person following the prompts."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL, PINE = 28520, 28521
PY = sys.executable
# the Linux simulator needs memfd/mmap; on Windows its port is used (and a real RPCS3's IPC is never touched)
SIM = "fake_rpcs3_win.py" if os.name == "nt" else "fake_rpcs3.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def ctl(cmd: str) -> str:
    with socket.create_connection(("127.0.0.1", CTL), timeout=5) as s:
        f = s.makefile("rw")
        f.write(cmd + "\n")
        f.flush()
        return f.readline().strip()


def stick_for(text: str):
    if "ВПРАВО" in text:
        return (0.6, 0, 0, 0)
    if "ВЛЕВО" in text:
        return (-0.6, 0, 0, 0)
    if "ВВЕРХ" in text:
        return (0, 0.6, 0, 0)
    if "ВНИЗ" in text:
        return (0, -0.6, 0, 0)
    if "ВПЕРЁД" in text:
        return (0, 0, 0, 0.6)
    return None


def main() -> int:
    sim = subprocess.Popen([PY, os.path.join(ROOT, "tests", SIM), "--seed", "5",
                            "--control-port", str(CTL), "--pine-port", str(PINE)], stdout=subprocess.PIPE, text=True,
                           env=ENV)
    truth = json.loads(sim.stdout.readline())
    out = os.path.join(tempfile.mkdtemp(prefix="kami_manual_"), "camfind.json")
    cf = subprocess.Popen([PY, "-X", "utf8", "camfind.py", "--backend", "process", "--pid", str(sim.pid),
                           "--pine", f"tcp:127.0.0.1:{PINE}", "--stimulus", "manual",
                           "--sweep", "3", "-o", out], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
    prompts = []

    def robot():
        pending = None
        for line in cf.stderr:
            line = line.rstrip("\n")
            if line.startswith(">>> "):
                text = line[4:]
                prompts.append(text)
                st = stick_for(text)
                if "Не трогайте" in text or st is None:
                    continue
                continuous = any(k in text for k in ("Вращайте", "Ведите", "Верните"))
                if continuous:
                    # "press Enter and start immediately": Enter first, then hold until told to stop
                    cf.stdin.write("\n")
                    cf.stdin.flush()
                    ctl("stick %s %s %s %s" % st)
                    pending = True
                else:
                    seconds = 0.75 if "ВЛЕВО" in text or "ВНИЗ" in text else 0.5
                    if "ВВЕРХ" in text and "исходному" in text:
                        seconds = 0.35
                    ctl("stick %s %s %s %s" % st)
                    time.sleep(seconds)
                    ctl("stick 0 0 0 0")
                    time.sleep(0.8)
                    cf.stdin.write("\n")
                    cf.stdin.flush()
            elif "СТОП" in line and pending:
                ctl("stick 0 0 0 0")
                pending = None

    t = threading.Thread(target=robot, daemon=True)
    t.start()
    stdout = cf.stdout.read()
    rc = cf.wait()
    ctl("quit")
    sim.wait(timeout=5)
    res = json.load(open(out)) if os.path.isfile(out) else {}
    ok = rc == 0 and res.get("hor") and int(res["hor"][0]["addr"], 16) == truth["hor_master"] \
        and res.get("vert") and int(res["vert"][0]["addr"], 16) == truth["vert_master"]
    print(f"prompts followed: {len(prompts)}")
    for p in prompts:
        print("   " + p)
    print(stdout[-900:])
    print("MANUAL MODE " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
