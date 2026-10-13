#!/usr/bin/env bash
# Runs every automated check of the toolkit. Linux, Python 3.9+, numpy; llvm-mc and a Lua 5.3
# interpreter are used when present (LUA53=/path/to/lua).
set -u
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
fail=0
step() { echo; echo "##### $1"; shift; "$@" || { echo "FAILED: $*"; fail=1; }; }

step "pipeline: camfind -> ptrscan -> restart -> verify -> mkprofile -> drive like KAMI (3 camera models)" \
     $PY tests/test_pipeline.py
step "camfind manual mode (scripted operator)" $PY tests/test_manual.py
TMP=$(mktemp -d)
if command -v llvm-mc >/dev/null; then
  step "test executable, encodings vs llvm-mc" $PY tests/make_test_elf.py "$TMP/test_game.elf" --check-encodings
else
  step "test executable" $PY tests/make_test_elf.py "$TMP/test_game.elf"
fi
step "ps3elf on the test executable" $PY tests/test_ps3elf.py "$TMP/test_game.elf"
step "gdbtrace against the stand-in stub" $PY tests/test_gdbtrace.py
step "RPCS3 cheat script equals DerefChain" $PY tests/test_cheat_script.py
if [ -n "${LUA53:-}" ]; then
  step "Cheat Engine script logic" "$LUA53" tests/ce_stub_test.lua cheatengine/rpcs3_kami.lua
fi
rm -rf "$TMP"
echo; [ $fail = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $fail
