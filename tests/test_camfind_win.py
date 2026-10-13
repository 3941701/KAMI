#!/usr/bin/env python3
"""End-to-end check of camfind on Windows against tests/fake_rpcs3_win.py.

The simulated game is made nasty on purpose:
  * 120 "decoy" words in low static data follow the yaw like an angle but are not the master
    (the old write test tried candidates in address order and stopped after 120);
  * when phase C starts, 300 yaw-derived words begin to flip between NaN, +-inf and +-3e38
    (what pointers / integers look like read as floats in a live game).

Checks: the master is found and is the best candidate, it kept the value written to it,
no numpy RuntimeWarning appears, the JSON is strict (no NaN), phase D prints progress and
ends in reasonable time.  Run:  py -3 tests/test_camfind_win.py [--script camfind.py.bak]
(--script with the old copy shows what used to happen; it is not expected to pass.)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL, PINE = 28530, 28531
PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def ctl(cmd: str) -> str:
    with socket.create_connection(("127.0.0.1", CTL), timeout=5) as s:
        f = s.makefile("rw")
        f.write(cmd + "\n")
        f.flush()
        return f.readline().strip()


def strict_json(path: str) -> dict:
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c} in {path}")
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read(), parse_constant=bad)


def run_case(script: str, model: str, decoys: int, glitch: bool, extra: list, timeout: float,
             known: bool = False, clamp: float = None) -> dict:
    sim = subprocess.Popen([PY, os.path.join(ROOT, "tests", "fake_rpcs3_win.py"), "--seed", "11", "--model", model,
                            "--decoys", str(decoys), "--control-port", str(CTL), "--pine-port", str(PINE)],
                           stdout=subprocess.PIPE, text=True, env=ENV)
    truth = json.loads(sim.stdout.readline())
    if known:                                      # --known-hor / --known-vert: skip the search, measure these
        extra = list(extra) + ["--known-hor", hex(truth["hor_master"])]
        if model != "hvvec":
            extra += ["--known-vert", hex(truth["vert_master"])]
    out = os.path.join(tempfile.mkdtemp(prefix="camfind_win_"), "camfind.json")
    cmd = [PY, "-X", "utf8", os.path.join(ROOT, script), "--backend", "process", "--pid", str(sim.pid),
           "--stimulus", "sim", "--sim-port", str(CTL), "--pine", f"tcp:127.0.0.1:{PINE}", "-o", out, *extra]
    t0 = time.time()
    cf = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", env=ENV)
    err_lines, marks = [], {}

    def pump_err():
        for line in cf.stderr:
            line = line.rstrip("\n")
            err_lines.append(line)
            for tag in ("[A]", "[B]", "[C]", "[D]"):
                if line.startswith(tag):
                    marks[tag] = time.time() - t0
            if clamp is not None and line.startswith("[D]"):
                try:                               # raise the minimum pitch above where the camera rests: the
                    ctl(f"limits {clamp} 1.2")     # game now clamps a push in that direction, like a real one
                except OSError as e:
                    err_lines.append(f"!!! test harness could not set pitch limits: {e}")
            if glitch and line.startswith("[C]"):
                try:
                    ctl("glitch 1")
                except OSError as e:
                    err_lines.append(f"!!! test harness could not enable glitch: {e}")

    out_buf = []
    threads = [threading.Thread(target=pump_err, daemon=True),
               threading.Thread(target=lambda: out_buf.append(cf.stdout.read()), daemon=True)]
    for t in threads:
        t.start()
    try:
        rc = cf.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        cf.kill()
        rc = "TIMEOUT"
    for t in threads:
        t.join(5)
    elapsed = time.time() - t0
    try:
        ctl("quit")
        sim.wait(timeout=5)
    except Exception:
        sim.kill()
    time.sleep(0.5)
    res = strict_json(out) if os.path.isfile(out) else {}
    return {"rc": rc, "res": res, "truth": truth, "stderr": err_lines, "stdout": "".join(out_buf),
            "elapsed": elapsed, "marks": marks}


def check(cond: bool, what: str, fails: list) -> None:
    print(("   ok   " if cond else "   FAIL ") + what)
    if not cond:
        fails.append(what)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default="camfind.py")
    ap.add_argument("--models", nargs="*", default=["hava"])
    ap.add_argument("--decoys", type=int, default=120)
    ap.add_argument("--no-glitch", action="store_true")
    ap.add_argument("--known", action="store_true", help="test --known-hor/--known-vert (no search phases)")
    ap.add_argument("--clamp-pitch", type=float, default=None, metavar="MIN",
                    help="when phase D starts, make the game clamp pitch to at least MIN (a push towards the "
                         "middle of the range then comes back clamped, as with a real camera limit)")
    ap.add_argument("--timeout", type=float, default=420)
    ap.add_argument("--quiet", action="store_true", help="do not echo camfind's log")
    args = ap.parse_args()

    fails: list = []
    for model in args.models:
        print(f"\n=== {args.script}, model {model}, decoys {args.decoys}, glitch {not args.no_glitch} ===")
        r = run_case(args.script, model, args.decoys, not args.no_glitch, [], args.timeout, args.known,
                     args.clamp_pitch)
        if not args.quiet:
            print("\n".join("   | " + ln for ln in r["stderr"]))
        res, truth = r["res"], r["truth"]
        warns = [ln for ln in r["stderr"] if "Warning" in ln]
        d_start = r["marks"].get("[D]")
        print(f"   rc={r['rc']} elapsed={r['elapsed']:.0f}s phase D started at "
              f"{d_start if d_start is None else round(d_start)}s, warnings: {len(warns)}")
        for w in warns[:6]:
            print("      " + w)
        check(r["rc"] == 0, "camfind exited with 0", fails)
        check(not warns, "no numpy RuntimeWarning in the log", fails)
        hor, vert = res.get("hor", []), res.get("vert", [])
        check(bool(hor), "horizontal candidates found", fails)
        if hor:
            h = hor[0]
            check(int(h["addr"], 16) == truth["hor_master"], f"best horizontal is the master ({h['addr']}, {h['kind']})", fails)
            expect = {"hava": "angle_rad", "hvecva": "vec2", "hvvec": "vec3"}[model]
            check(h["kind"] == expect, f"classified as {expect}", fails)
            check(h.get("sticky") is True, "horizontal master kept the written value", fails)
        if model != "hvvec":                      # a direction vector has no separate vertical master
            check(bool(vert) and int(vert[0]["addr"], 16) == truth["vert_master"], "best vertical is the master", fails)
            if vert:
                check(vert[0].get("sticky") is True, "vertical master kept the written value", fails)
        check(any("[D " in ln for ln in r["stderr"]), "phase D printed per-candidate progress", fails)
        wt = res.get("write_test", {})
        check(wt.get("tested", 0) > 0 and wt.get("write_errors", 1) == 0, f"write test ran, no write errors ({wt})", fails)

    print("\n" + ("ALL PASSED" if not fails else f"FAILED: {len(fails)} check(s)"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
