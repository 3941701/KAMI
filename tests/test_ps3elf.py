#!/usr/bin/env python3
"""Checks ps3elf.py against the executable built by make_test_elf.py."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
elf = sys.argv[1]
truth = json.load(open(elf + ".truth.json"))
L = {k: int(v, 16) for k, v in truth["labels"].items()}


def run(*args, ok=(0,)):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ps3elf.py"), *args], capture_output=True, text=True)
    assert r.returncode in ok, (args, r.returncode, r.stdout, r.stderr)
    return r.stdout


def check(cond, what):
    print(("   ok   " if cond else "   FAIL ") + what)
    if not cond:
        sys.exit(1)


out = run("info", elf)
check(f"TOC {truth['toc'].replace('0x', '0x')}" in out.replace("TOC 0x", "TOC 0x"), "TOC read from the entry descriptor")
out = run("imports", elf)
check("cellPadGetData" in out and f"0x{L['cellPadGetData_stub']:08X}" in out, "import resolved by NID to its stub")
out = run("callers", elf, "--depth", "4")
lines = [l for l in out.splitlines() if "fn 0x" in l]
check([int(l.split("fn 0x")[1][:8], 16) for l in lines] == [L["input_poll"], L["game_frame"], L["start"]],
      "pad reader call tree: input_poll <- game_frame <- start")
findings = elf + ".findings.json"
if os.path.exists(findings):
    os.remove(findings)
out = run("fieldxref", elf, "--offsets", "0x1A8,0x1C4", "--ops", "stfs", "--min-match", "2", "--ghidra", findings)
funcs = [int(l.split("fn 0x")[1][:8], 16) for l in out.splitlines() if l.startswith("fn 0x")]
check(funcs == [L["camera_update"]], "the only function storing to both offsets is camera_update")
check(truth["vtable_slot"].upper().replace("0X", "0x") in out, "its descriptor is found in the vtable slot")
items = json.load(open(findings))["items"]
check(any(i["label"].startswith("kami_field_user_") for i in items) and len(items) == 3, "findings file written")
out = run("fieldxref", elf, "--offsets", "0x1A8,0x1C4")
check(f"fn 0x{L['camera_read']:08X}" in out and f"fn 0x{L['camera_update']:08X}" in out, "readers are listed too")
out = run("dataxref", elf, "--addr", truth["fov"])
check(f"fn 0x{L['fov_user']:08X}" in out and "lis at" in out, "absolute address found through lis + low half")
out = run("dataxref", elf, "--addr", "0x00011000")
check(f"fn 0x{L['camera_update']:08X}" in out and "via pointer" in out, "constant found through its TOC entry")
out = run("floats", elf, "--min-match", "2")
check(f"fn 0x{L['camera_update']:08X}: 2pi x1, pi x1" in out, "function loading pi and 2*pi identified")
patch = elf + ".patch.yml"
run("fieldxref", elf, "--offsets", "0x1A8,0x1C4", "--bp-patch", patch, "--hash", "PPU-" + "0" * 40)
text = open(patch).read()
check(text.count("bpex") == 2 and "Version: 1.2" in text, "RPCS3 breakpoint patch written for the two stores")
print("PS3ELF OK")
