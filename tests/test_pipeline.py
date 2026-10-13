#!/usr/bin/env python3
"""End-to-end test of the toolkit against tests/fake_rpcs3.py.

For each of KAMI's three camera models the test runs the real command-line
tools, in the order a person would:

  session 1   camfind (automatic stimulus)  ->  ptrscan find
  session 2   (game "restarted": every heap address differs)
              ptrscan verify  ->  mkprofile make  ->  mkprofile check
              then drives the profile the way KAMI does and checks, against the
              simulator's ground truth, that mouse-right turns the camera right,
              mouse-down looks down, and the pitch limit holds.

Run:  python tests/test_pipeline.py [model ...]        (Linux only)
"""
from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from rpcs3mem.backend import PineMemory            # noqa: E402
from rpcs3mem.procmem import ProcessMemory          # noqa: E402
from rpcs3mem.profile import Profile                # noqa: E402

CTL, PINE = 28510, 28511
PY = sys.executable


class Sim:
    def __init__(self, seed: int, model: str):
        self.proc = subprocess.Popen([PY, os.path.join(ROOT, "tests", "fake_rpcs3.py"), "--seed", str(seed),
                                      "--model", model, "--control-port", str(CTL), "--pine-port", str(PINE)],
                                     stdout=subprocess.PIPE, text=True)
        self.first = json.loads(self.proc.stdout.readline())
        self.pid = str(self.proc.pid)

    def ctl(self, cmd: str) -> str:
        with socket.create_connection(("127.0.0.1", CTL), timeout=5) as s:
            f = s.makefile("rw")
            f.write(cmd + "\n")
            f.flush()
            return f.readline().strip()

    def truth(self) -> dict:
        return json.loads(self.ctl("get"))

    def stop(self) -> None:
        try:
            self.ctl("quit")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        time.sleep(0.3)


def run(*args: str) -> str:
    r = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"{' '.join(args)}\nexit {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def angle_diff(a: float, b: float) -> float:
    d = a - b
    return d - 2 * math.pi * round(d / (2 * math.pi))


def check(cond: bool, what: str) -> None:
    print(("   ok   " if cond else "   FAIL ") + what)
    if not cond:
        raise AssertionError(what)


def test_model(model: str, tmp: str) -> None:
    print(f"\n=== model {model} ===")
    cf, ch, prof = (os.path.join(tmp, f"{model}_{n}") for n in ("camfind.json", "chains.json", "profile.json"))
    pine = f"tcp:127.0.0.1:{PINE}"

    sim = Sim(seed=11, model=model)
    try:
        t1 = sim.truth()
        run("camfind.py", "--backend", "process", "--pid", sim.pid, "--stimulus", "sim", "--sim-port", str(CTL),
            "--pine", pine, "-o", cf)
        res = json.load(open(cf))
        check(bool(res["hor"]), "camfind found horizontal candidates")
        best_h = res["hor"][0]
        check(int(best_h["addr"], 16) == t1["hor_master"],
              f"best horizontal candidate is the master ({best_h['addr']}, {best_h['kind']})")
        check(best_h.get("sticky") is True, "horizontal master kept a written value")
        check(res["game"].get("id") == "BCUS98119", "game identity came through PINE")
        expect_kind = {"hava": "angle_rad", "hvecva": "vec2", "hvvec": "vec3"}[model]
        check(best_h["kind"] == expect_kind, f"classified as {expect_kind}")
        targets = ["--target", f"hor={best_h['addr']}"]
        if model != "hvvec":
            best_v = res["vert"][0]
            check(int(best_v["addr"], 16) == t1["vert_master"], f"best vertical candidate is the master ({best_v['addr']})")
            check(best_v.get("clamp_lo") and best_v.get("clamp_hi"), "pitch limits detected")
            check(abs(best_v["lo"] - t1["pitch_min"]) < 0.02 and abs(best_v["hi"] - t1["pitch_max"]) < 0.02,
                  f"pitch limits correct ({best_v['lo']:.3f} .. {best_v['hi']:.3f})")
            targets += ["--target", f"vert={best_v['addr']}"]
        run("ptrscan.py", "find", "--backend", "process", "--pid", sim.pid, *targets, "-o", ch)
        n1 = len(json.load(open(ch))["chains"])
        check(n1 >= 2, f"ptrscan found {n1} chains")
    finally:
        sim.stop()

    sim = Sim(seed=29, model=model)
    try:
        t2 = sim.truth()
        check(t2["cam"] != t1["cam"], "second session moved the camera object")
        run("ptrscan.py", "verify", "--backend", "process", "--pid", sim.pid, "--chains", ch)
        doc = json.load(open(ch))
        check(1 <= len(doc["chains"]) < n1, f"{len(doc['chains'])} of {n1} chains survived the restart")
        run("mkprofile.py", "make", "--camfind", cf, "--chains", ch, "-o", prof, "--replace")
        out = run("mkprofile.py", "check", "--backend", "process", "--pid", sim.pid, "--pine", pine, "--profile", prof)
        check(f"0x{t2['hor_master']:08X}" in out, "profile resolves to the new master address")

        profile = Profile.load(prof)
        build = profile.select("BCUS98119", "01.00", t2["hash"])
        check(build is not None, "profile matches the game by hash")
        sens = 0.003
        for backend_name in ("process", "pine"):
            mem = ProcessMemory(int(sim.pid)) if backend_name == "process" else PineMemory(pine)
            try:
                a = sim.truth()
                for _ in range(20):
                    build.update(mem, 10 * sens, 0)          # mouse 200 px to the right
                    time.sleep(0.01)
                time.sleep(0.2)
                b = sim.truth()
                turned = angle_diff(b["yaw_true"], a["yaw_true"])
                # in the simulator turning right DEcreases yaw
                check(abs(turned + 200 * sens) < 0.03, f"[{backend_name}] mouse right turned the camera right by "
                      f"{-turned:.3f} rad (wanted {200 * sens:.3f})")
                for _ in range(20):
                    build.update(mem, 0, 5 * sens)           # mouse 100 px down
                    time.sleep(0.01)
                time.sleep(0.2)
                c = sim.truth()
                check(abs((c["pitch_true"] - b["pitch_true"]) + 100 * sens) < 0.03,
                      f"[{backend_name}] mouse down looked down by {b['pitch_true'] - c['pitch_true']:.3f} rad")
                for _ in range(40):
                    build.update(mem, 0, -40 * sens)         # a long way up: must stop at the game's limit
                    time.sleep(0.005)
                time.sleep(0.2)
                d = sim.truth()
                check(d["pitch_true"] <= t2["pitch_max"] + 1e-3, f"[{backend_name}] pitch stayed within the limit "
                      f"({d['pitch_true']:.3f} <= {t2['pitch_max']})")
                for _ in range(20):
                    build.update(mem, 0, 20 * sens)
                    time.sleep(0.005)
            finally:
                mem.close()
        cs = run("mkprofile.py", "cs", "--profile", prof, "--class", "InFamous")
        check("class InFamous : Game<" in cs and "BCUS98119" in cs, "C# class generated")
        with open(os.path.join(tmp, f"{model}_InFamous.cs"), "w") as f:
            f.write(cs)
    finally:
        sim.stop()


def main() -> int:
    models = sys.argv[1:] or ["hava", "hvecva", "hvvec"]
    tmp = tempfile.mkdtemp(prefix="kami_toolkit_test_")
    keep = os.environ.get("KEEP_TEST_OUTPUT")
    try:
        for m in models:
            test_model(m, tmp)
        print("\nALL PASSED")
        return 0
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        keep = True
        return 1
    finally:
        if keep:
            print(f"(outputs kept in {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
