"""Ways to move the in-game camera while a scan is running.

manual  - prints an instruction and waits for you to do it (default)
vigem   - drives a virtual Xbox 360 pad through ViGEm (Windows, ``pip install
          vgamepad``), so a whole scan runs hands-free.  In RPCS3 set Player 1
          to the XInput handler and make sure the virtual pad is the device
          it is bound to (unplug other XInput pads, or pick the right index).
sim     - talks to tests/fake_rpcs3.py

Conventions used by the tools: yaw +1 = turn the camera right, pitch +1 =
look up, as produced by pushing the right stick right / up.
"""
from __future__ import annotations

import os
import socket
import sys
import time


class Stimulus:
    automatic = False
    name = "manual"

    def idle(self, seconds: float, why: str = "") -> None:
        raise NotImplementedError

    def move(self, yaw: float, pitch: float, seconds: float, text: str) -> None:
        """Deflect the camera stick for ``seconds`` then release (blocking)."""
        raise NotImplementedError

    def begin(self, yaw: float, pitch: float, text: str) -> None:
        """Start a continuous motion; the caller samples memory until end()."""
        raise NotImplementedError

    def end(self) -> None:
        raise NotImplementedError

    def walk(self, seconds: float, text: str) -> None:
        """Move the character without touching the camera."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class ManualStimulus(Stimulus):
    def __init__(self, out=sys.stderr):
        self.out = out

    def _ask(self, text: str) -> None:
        print(f"\n>>> {text}", file=self.out, flush=True)
        input("    Сделайте это и нажмите Enter... ")

    def idle(self, seconds, why=""):
        print(f"\n>>> Не трогайте управление{': ' + why if why else ''} ({seconds:.1f} c)...",
              file=self.out, flush=True)
        time.sleep(seconds)

    def move(self, yaw, pitch, seconds, text):
        self._ask(text)

    def begin(self, yaw, pitch, text):
        print(f"\n>>> {text}", file=self.out, flush=True)
        input("    Нажмите Enter и СРАЗУ начинайте; продолжайте, пока не появится «стоп»... ")

    def end(self):
        print("    СТОП - отпустите стик.", file=self.out, flush=True)
        time.sleep(1.0)

    def walk(self, seconds, text):
        self._ask(text)


def _beep(n: int = 1, long: bool = False) -> None:
    if os.environ.get("KAMI_NO_BEEP"):
        return
    try:
        import winsound
        for _ in range(n):
            winsound.Beep(700 if long else 1000, 450 if long else 120)
            time.sleep(0.05)
    except Exception:
        print("\a", end="", flush=True, file=sys.stderr)


class TimedStimulus(Stimulus):
    """Like 'manual', but nothing waits for Enter: every request opens a time window, announced and closed by
    a beep, and the scan simply carries on.  For states that do not survive a trip to the console window
    (holding the aim button, say): stay in the game the whole time and just follow the beeps.

      move   : text, one beep = go, you have ``prompt`` seconds, two beeps = stop
      begin  : text, one beep = get ready (``lead`` seconds), a long beep = recording starts and runs until
               two beeps (the caller decides how long)
    """
    name = "timed"

    def __init__(self, prompt: float = 6.0, lead: float = 2.5, out=sys.stderr):
        self.prompt, self.lead, self.out = prompt, lead, out

    def warmup(self, seconds: float) -> None:
        """Time to switch to the game window and get into the state that is to be scanned."""
        print(f"\nУ вас {seconds:.0f} с: перейдите в окно игры и приведите игру в нужное состояние "
              f"(например, зажмите прицел и держите его до конца). Дальше консоль не трогайте.", file=self.out, flush=True)
        time.sleep(seconds)
        _beep(3)

    def idle(self, seconds, why=""):
        print(f"\n>>> Не трогайте управление{': ' + why if why else ''} ({seconds:.1f} c)...", file=self.out, flush=True)
        time.sleep(seconds)

    def move(self, yaw, pitch, seconds, text):
        print(f"\n>>> {text}", file=self.out, flush=True)
        print(f"    Сигнал - начинайте; на это {self.prompt:.0f} c; два сигнала - стоп. Окно консоли трогать не нужно.",
              file=self.out, flush=True)
        _beep(1)
        time.sleep(self.prompt)
        _beep(2)
        time.sleep(1.0)

    def begin(self, yaw, pitch, text):
        print(f"\n>>> {text}", file=self.out, flush=True)
        print(f"    Сигнал - приготовьтесь; через {self.lead:.1f} c длинный сигнал: запись пошла, продолжайте до двух сигналов.",
              file=self.out, flush=True)
        _beep(1)
        time.sleep(self.lead)
        _beep(1, long=True)

    def end(self):
        print("    СТОП - отпустите стик.", file=self.out, flush=True)
        _beep(2)
        time.sleep(1.0)

    def walk(self, seconds, text):
        self.move(0, 0, seconds, text)


class _StickStimulus(Stimulus):
    """Shared timing logic for stimuli that can set the sticks programmatically."""
    automatic = True
    deflection = 0.6
    settle = 1.0

    def _set(self, rx: float, ry: float, lx: float = 0.0, ly: float = 0.0) -> None:
        raise NotImplementedError

    def idle(self, seconds, why=""):
        self._set(0, 0)
        time.sleep(seconds)

    def move(self, yaw, pitch, seconds, text):
        print(f"[stimulus] {text}", file=sys.stderr, flush=True)
        self._set(yaw * self.deflection, pitch * self.deflection)
        time.sleep(seconds)
        self._set(0, 0)
        time.sleep(self.settle)

    def begin(self, yaw, pitch, text):
        print(f"[stimulus] {text}", file=sys.stderr, flush=True)
        self._set(yaw * self.deflection, pitch * self.deflection)

    def end(self):
        self._set(0, 0)
        time.sleep(self.settle)

    def walk(self, seconds, text):
        print(f"[stimulus] {text}", file=sys.stderr, flush=True)
        self._set(0, 0, 0, self.deflection)
        time.sleep(seconds)
        self._set(0, 0)
        time.sleep(self.settle)

    def close(self):
        try:
            self._set(0, 0)
        except Exception:
            pass


class VigemStimulus(_StickStimulus):
    name = "vigem"

    def __init__(self, deflection: float = 0.6):
        try:
            import vgamepad as vg
        except ImportError as e:
            raise RuntimeError("the vigem stimulus needs the 'vgamepad' package "
                               "(pip install vgamepad; installs the ViGEmBus driver)") from e
        self.deflection = deflection
        self.pad = vg.VX360Gamepad()
        self._set(0, 0)
        time.sleep(1.5)            # give Windows and RPCS3 time to see the new device

    def _set(self, rx, ry, lx=0.0, ly=0.0):
        self.pad.right_joystick_float(x_value_float=float(rx), y_value_float=float(ry))
        self.pad.left_joystick_float(x_value_float=float(lx), y_value_float=float(ly))
        self.pad.update()


class SimStimulus(_StickStimulus):
    name = "sim"
    settle = 0.5

    def __init__(self, port: int = 28500, deflection: float = 0.6):
        self.deflection = deflection
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.sock.makefile("rw")

    def _set(self, rx, ry, lx=0.0, ly=0.0):
        self.f.write(f"stick {rx} {ry} {lx} {ly}\n")
        self.f.flush()
        self.f.readline()

    def close(self):
        super().close()
        self.sock.close()


def make_stimulus(kind: str, **kw) -> Stimulus:
    if kind == "manual":
        return ManualStimulus()
    if kind == "timed":
        return TimedStimulus(kw.get("prompt", 6.0), kw.get("lead", 2.5))
    if kind == "vigem":
        return VigemStimulus(kw.get("deflection", 0.6))
    if kind == "sim":
        return SimStimulus(kw.get("port", 28500), kw.get("deflection", 0.6))
    raise ValueError(f"unknown stimulus {kind!r}")
