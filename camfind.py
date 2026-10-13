#!/usr/bin/env python3
"""camfind - locate the camera yaw / pitch values of a game running in RPCS3.

The search is the usual "unknown value" hunt, but scripted end to end:

  A. whole-memory snapshots: drop everything that changes while nothing is
     happening, keep what changes when the camera yaws, then what changes when
     it pitches;
  B. repeated yaw / pitch / idle (/ walk) steps on the survivors only;
  C. continuous traces while the camera sweeps: decide what each survivor *is*
     (angle in radians or degrees, sin/cos pair, direction vector), which way
     it moves when turning right / looking up, and its limits;
  D. write test: nudge the best candidates and watch.  The value the game
     itself integrates ("master") keeps the new value and drags the derived
     copies along; a derived copy is simply overwritten on the next frame.

The result is a ranked list in JSON, ready for ptrscan.py and mkprofile.py.

Camera motion comes from --stimulus: 'manual' (you move the stick when asked),
'vigem' (a virtual pad does it, Windows) or 'sim' (test simulator).

Raw memory words are read as big-endian float32; whatever is not a plausible
number (NaN, inf, |x| > 1e5: integers, pointers, garbage) is turned into NaN
right at the read and never takes part in any arithmetic.
Ctrl+C is safe at any point: values written by the write test are put back and
whatever has been found so far is saved.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from rpcs3mem import add_backend_args, open_backend, parse_int, parse_range, pine_info
from rpcs3mem.common import MemoryError_
from rpcs3mem.scan import (Snapshot, as_float, changed_mask, mask_to_addrs, plausible_float,
                           read_words, scan_regions, words_at)
from rpcs3mem.stimulus import make_stimulus

TWO_PI = 2.0 * math.pi
NAN32 = np.float32("nan")
# kinds that classification can give a candidate (and the "_part" halves of pairs)
CAM_KINDS = ("angle_rad", "angle_deg", "vec2", "vec3")
PROBE_KINDS = CAM_KINDS + ("vec2_part", "vec3_part")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sane(f: np.ndarray) -> np.ndarray:
    """float32 copy where everything that cannot be a camera number is NaN.

    NaN, +-inf and |x| > 1e5 (what integers and pointers look like read as
    floats) are removed here, before any subtraction / square / int cast, so
    nothing downstream can overflow.  NaN then simply compares false.
    """
    return np.where(plausible_float(f), f, NAN32).astype(np.float32)


class WriteUnavailable(Exception):
    """Writing to the game's memory does not work at all (every attempt failed)."""


class NothingLeft(Exception):
    """No candidate survived a phase; the message says what to try."""


def write_error_hint(err: str) -> str:
    m = re.search(r"error (\d+)", err)
    code = int(m.group(1)) if m else None
    if code == 5:
        return ("нет прав на запись в процесс rpcs3 (ACCESS_DENIED): запустите терминал от имени "
                "администратора (обязательно, если rpcs3 запущен от администратора)")
    if code in (299, 998):
        return "страница памяти не принимает запись (PARTIAL_COPY / NOACCESS)"
    if "read-only" in err:
        return "источник памяти открыт только для чтения"
    if "другое значение" in err:
        return ("запись выполняется, но в памяти остаётся не то, что записано: адрес не принадлежит живому "
                "объекту игры либо запись идёт не в то отображение памяти")
    return "см. текст ошибки выше"


# --------------------------------------------------------------------------
# Candidate table
# --------------------------------------------------------------------------
class Table:
    """Surviving addresses with their per-step bookkeeping."""

    def __init__(self, addrs: np.ndarray, values: np.ndarray, h_ok: np.ndarray, v_ok: np.ndarray):
        self.addrs = addrs
        self.last = values                     # float32, value after the latest step
        self.h_ok = h_ok.copy()                # changed in every yaw step so far
        self.v_ok = v_ok.copy()                # changed in every pitch step so far
        self.h_any = h_ok.copy()               # changed in at least one yaw step
        self.v_any = v_ok.copy()
        self.walk = np.zeros(len(addrs), dtype=bool)
        self.right_votes = np.zeros(len(addrs), dtype=np.int32)   # +1: grows when turning right
        self.up_votes = np.zeros(len(addrs), dtype=np.int32)

    def __len__(self) -> int:
        return len(self.addrs)

    def keep(self, mask: np.ndarray) -> None:
        for name in ("addrs", "last", "h_ok", "v_ok", "h_any", "v_any", "walk", "right_votes", "up_votes"):
            setattr(self, name, getattr(self, name)[mask])

    def cls(self) -> np.ndarray:
        """'H' yaw only, 'V' pitch only, 'B' both, '?' inconsistent."""
        out = np.full(len(self.addrs), "?", dtype="<U1")
        out[self.h_ok & ~self.v_any] = "H"
        out[self.v_ok & ~self.h_any] = "V"
        out[self.h_ok & self.v_ok] = "B"
        return out


class Probe:
    """The few table rows read around every write: the classified candidates (their derived
    copies react to a master write) and an evenly spaced sample of the rest, which stands in
    for the whole table (reading 30000 scattered words costs ~0.6 s; this costs ~30 ms)."""

    def __init__(self, cls_idx: np.ndarray, samp_idx: np.ndarray, weight: float, n_noisy: int):
        self.cls, self.samp, self.weight, self.n_noisy = cls_idx, samp_idx, weight, n_noisy


class CamFinder:
    def __init__(self, mem, stim, args):
        self.mem, self.stim, self.a = mem, stim, args
        lo, hi = args.range
        self.regions = scan_regions(mem, lo, hi)
        if not self.regions:
            raise SystemExit("в запрошенном диапазоне нет читаемой памяти гостя - запущена ли игра?")
        self.total = sum(r.size for r in self.regions)
        self.notes: List[str] = []
        self.t: Optional[Table] = None
        self.watch = [int(x) for x in (args.debug_addr or [])]
        self.interrupted = False
        self.static: List[Tuple[int, int]] = []        # the executable's own data: same addresses on every start
        self.checkpoint: Optional[Callable[[dict], None]] = None
        self.pos: Dict[int, int] = {}
        self.write_stats = {"tested": 0, "sticky": 0, "write_errors": 0, "write_lost": 0,
                            "skipped": 0, "seconds": 0.0}

    def debug(self, stage: str) -> None:
        """--debug-addr: say what happened to specific addresses (why was X not found?)."""
        for addr in self.watch:
            t = self.t
            hit = np.flatnonzero(t.addrs == addr) if t is not None else []
            if len(hit) == 0:
                log(f"    [debug 0x{addr:08X}] {stage}: НЕ в списке кандидатов")
            else:
                i = int(hit[0])
                log(f"    [debug 0x{addr:08X}] {stage}: value={t.last[i]:.6f} h_ok={bool(t.h_ok[i])} "
                    f"v_ok={bool(t.v_ok[i])} h_any={bool(t.h_any[i])} v_any={bool(t.v_any[i])} "
                    f"right={int(t.right_votes[i])} up={int(t.up_votes[i])}")

    # -- helpers ----------------------------------------------------------
    def full(self, what: str) -> Snapshot:
        t0 = time.time()
        s = Snapshot(self.mem, self.regions)
        log(f"    снимок памяти ({what}): {self.total / 1048576:.0f} МиБ за {time.time() - t0:.2f} c")
        return s

    def current(self, idx: Optional[np.ndarray] = None) -> np.ndarray:
        """Live values of the table rows ``idx`` (all rows by default); non-numbers are NaN."""
        addrs = self.t.addrs if idx is None else self.t.addrs[idx]
        return sane(as_float(read_words(self.mem, addrs)))

    def _diff(self, a: Snapshot, b: Snapshot, noisy) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        masks = [m & ~n for m, n in zip(changed_mask(a, b), noisy)]
        addrs = mask_to_addrs(self.regions, masks)
        fa, fb = as_float(words_at(a, addrs)), as_float(words_at(b, addrs))
        ok = plausible_float(fa) & plausible_float(fb)          # filter first, subtract afterwards
        addrs, fa, fb = addrs[ok], fa[ok], fb[ok]
        with np.errstate(all="ignore"):
            moved = np.abs(fb - fa) > self.a.eps_move
        return addrs[moved], fa[moved], fb[moved]

    @staticmethod
    def _lookup(keys: np.ndarray, values: np.ndarray, wanted: np.ndarray) -> np.ndarray:
        """values[keys == wanted] with 0 where absent (keys sorted ascending)."""
        out = np.zeros(len(wanted), dtype=np.int32)
        if len(keys) == 0:
            return out
        pos = np.minimum(np.searchsorted(keys, wanted), len(keys) - 1)
        hit = keys[pos] == wanted
        out[hit] = values[pos[hit]].astype(np.int32)
        return out

    def _require(self, stage: str) -> None:
        if self.t is None or len(self.t) == 0:
            raise NothingLeft(stage)

    # -- phase A: whole-memory diffs --------------------------------------
    def phase_a(self) -> None:
        a = self.a
        log("\n[A] Полные снимки памяти")
        log("    Сначала два снимка в покое (всё, что меняется само, отбрасывается), затем поворот вправо и наклон вверх.")
        self.stim.idle(a.settle, "базовый снимок")
        f0 = self.full("покой 1")
        time.sleep(a.idle_gap)
        f1 = self.full("покой 2")
        noisy = changed_mask(f0, f1)
        n_noisy = int(sum(int(m.sum()) for m in noisy))
        del f0
        log(f"    меняется само по себе: {n_noisy} слов - исключены")

        self.stim.move(+1, 0, a.step_time, "Поверните камеру ВПРАВО примерно на 30-60° и отпустите стик.")
        f2 = self.full("после поворота вправо")
        h_addrs, h_before, h_after = self._diff(f1, f2, noisy)
        del f1
        log(f"    изменилось при повороте: {len(h_addrs)}")

        self.stim.move(0, +1, a.step_time * 0.6, "Наклоните камеру ВВЕРХ (немного, не до упора) и отпустите стик.")
        f3 = self.full("после наклона вверх")
        v_addrs, v_before, v_after = self._diff(f2, f3, noisy)
        log(f"    изменилось при наклоне: {len(v_addrs)}")

        addrs = np.union1d(h_addrs, v_addrs)
        in_h = np.isin(addrs, h_addrs, assume_unique=True)
        in_v = np.isin(addrs, v_addrs, assume_unique=True)
        vals_f2 = as_float(words_at(f2, addrs))
        vals_f3 = as_float(words_at(f3, addrs))
        del f2, f3
        ok = plausible_float(vals_f2) & plausible_float(vals_f3)
        self.t = Table(addrs[ok], vals_f3[ok], in_h[ok], in_v[ok])
        # direction votes from the two motions just made
        with np.errstate(all="ignore"):
            self.t.right_votes += self._lookup(h_addrs, np.sign(h_after - h_before), self.t.addrs)
            self.t.up_votes += self._lookup(v_addrs, np.sign(v_after - v_before), self.t.addrs)
        log(f"    кандидатов после фазы A: {len(self.t)}")
        self.debug("фаза A")
        if len(self.t) > a.max_candidates:
            self.notes.append(f"phase A left {len(self.t)} candidates (limit {a.max_candidates}); "
                              f"the scene is very busy - try a quieter spot or pause other motion")
        self._require("после фазы A не осталось ни одного кандидата")

    def seed_known(self) -> None:
        """--known-hor / --known-vert: the addresses are already known (found earlier, checked by hand), so
        phases A and B are skipped: the table is just these words (plus the two words after a horizontal
        one, in case it is a sin/cos pair or a direction vector) and phase C measures them as usual."""
        a = self.a
        log("\n[A, B пропущены] Адреса заданы вручную: беру только их и сразу перехожу к трассировке.")
        rows: Dict[int, Tuple[bool, bool]] = {}
        known_h, known_v = list(a.known_hor or []), list(a.known_vert or [])
        for h in known_h:
            for off in (0, 4, 8):
                rows[h + off] = (True, False)
        for v in known_v:
            rows[v] = (False, True)                    # a vertical address wins over a horizontal "partner word"
        addrs = np.array(sorted(rows), dtype=np.uint32)
        vals = sane(as_float(read_words(self.mem, addrs)))
        alive = 0
        for m in known_h + known_v:
            v = vals[int(np.flatnonzero(addrs == m)[0])]
            if not np.isfinite(v):
                log(f"    0x{m:08X}: сейчас не число с плавающей точкой - пропускаю (адрес устарел? "
                    f"объекты в куче живут только в одном сеансе игры)")
                continue
            alive += 1
            log(f"    0x{m:08X} = {float(v):.5f}")
        if alive == 0:
            raise NothingLeft("ни один из заданных адресов сейчас не читается как число (адреса устарели?)")
        ok = np.isfinite(vals)
        self.t = Table(addrs[ok], vals[ok],
                       np.array([rows[int(x)][0] for x in addrs[ok]], dtype=bool),
                       np.array([rows[int(x)][1] for x in addrs[ok]], dtype=bool))

    # -- phase B: steps on the survivors ----------------------------------
    def _step(self, kind: str, direction: int) -> None:
        t, a = self.t, self.a
        cur = self.current()
        with np.errstate(all="ignore"):
            d = cur - t.last
            changed = np.abs(d) > a.eps_move
            same = np.abs(d) <= a.eps_same
            readable = np.isfinite(cur)                 # NaN here = unreadable or not a plausible number
            vote = np.where(changed, np.sign(d) * direction, 0).astype(np.int32)   # no NaN reaches the cast
        if kind == "idle":
            keep = same & readable
        elif kind == "yaw":
            t.h_ok &= changed
            t.h_any |= changed
            # a wrap (jump of about a full turn) says nothing about direction
            small = changed & (np.abs(d) < 3.0)
            t.right_votes += np.where(small, vote, 0).astype(np.int32)
            keep = readable & (t.h_ok | t.v_ok)
        elif kind == "pitch":
            t.v_ok &= changed
            t.v_any |= changed
            t.up_votes += vote
            keep = readable & (t.h_ok | t.v_ok)
        elif kind == "walk":
            t.walk |= changed
            keep = readable
        else:
            raise ValueError(kind)
        t.last = cur
        t.keep(keep)
        log(f"    {kind:5s} {'+' if direction > 0 else '-' if direction < 0 else ' '} -> осталось {len(t)}")
        self.debug(f"шаг {kind}{'+' if direction > 0 else '-' if direction < 0 else ''}")
        self._require(f"после шага «{kind}» не осталось ни одного кандидата")

    def phase_b(self) -> None:
        a, s = self.a, self.stim
        log("\n[B] Шаги по оставшимся кандидатам")
        log("    Повторяем вправо / влево / вниз / вверх и проверки покоя; в списке остаются только значения,")
        log("    которые меняются при каждом таком движении и стоят на месте в покое.")
        s.idle(a.settle, "проверка покоя")
        self._step("idle", 0)
        for rnd in range(a.rounds):
            s.move(+1, 0, a.step_time, "Ещё раз ВПРАВО на 30-60° и отпустите.")
            self._step("yaw", +1)
            s.idle(a.settle, "проверка покоя")
            self._step("idle", 0)
            s.move(-1, 0, a.step_time * 1.5, "Теперь ВЛЕВО, чуть дальше исходного положения, и отпустите.")
            self._step("yaw", -1)
            s.move(0, -1, a.step_time * 1.2, "Наклоните камеру ВНИЗ (ниже исходного, не до упора) и отпустите.")
            self._step("pitch", -1)
            s.idle(a.settle, "проверка покоя")
            self._step("idle", 0)
            s.move(0, +1, a.step_time * 0.6, "Наклоните камеру ВВЕРХ примерно к исходному положению и отпустите.")
            self._step("pitch", +1)
            if len(self.t) <= a.enough:
                break
        if not a.no_walk:
            s.walk(a.step_time, "Пройдите персонажем ПРЯМО ВПЕРЁД пару шагов, НЕ трогая камеру, и остановитесь.")
            self._step("walk", 0)
            s.idle(a.settle, "проверка покоя")
            self._step("idle", 0)

    # -- phase C: traces --------------------------------------------------
    def _priority_order(self) -> np.ndarray:
        """Table rows, most promising first: clean yaw-only / pitch-only classes, then rows whose
        direction votes were consistent through all the steps, then by address."""
        t = self.t
        cls = t.cls()
        base = np.select([cls == "H", cls == "V", cls == "B"], [0, 0, 1], 2)
        cons = np.where(cls == "H", np.abs(t.right_votes),
                        np.where(cls == "V", np.abs(t.up_votes), np.abs(t.right_votes) + np.abs(t.up_votes)))
        return np.lexsort((t.addrs, -cons, base))

    def _trim(self, n: int, why: str) -> None:
        t = self.t
        if n >= len(t):
            return
        keep = np.zeros(len(t), dtype=bool)
        keep[self._priority_order()[:n]] = True
        before = len(t)
        t.keep(keep)
        self.notes.append(f"trace limited to {n} of {before} candidates ({why})")
        log(f"    для трассировки оставлено {n} из {before} кандидатов ({why})")

    def _fit_trace(self) -> None:
        """Measure how long one read of the table takes; scattered candidates are slow to read and a
        trace sampled at 1-2 Hz is useless, so cut the table (best first) to a readable size."""
        t, a = self.t, self.a
        best = 1e9
        for _ in range(2):
            t0 = time.time()
            self.current()
            best = min(best, time.time() - t0)
        self.read_s = best
        log(f"    одно чтение {len(t)} кандидатов занимает {best * 1000:.0f} мс "
            f"(примерно {1.0 / max(best, 1e-3):.1f} отсчётов в секунду)")
        limit_s = a.trace_ms / 1000.0
        if best > limit_s and len(t) > a.trace_min:
            self._trim(max(a.trace_min, int(len(t) * limit_s / best)),
                       f"чтение дольше {a.trace_ms:.0f} мс; увеличьте --trace-ms, чтобы оставить больше")

    def _trace(self, yaw: float, pitch: float, seconds: float, text: str, min_rows: int = 0) -> np.ndarray:
        rows = []
        self.stim.begin(yaw, pitch, text)
        t0 = time.time()
        t_end = t0 + seconds
        hard_end = t0 + max(seconds * 3.0, seconds + 10.0)
        period = 1.0 / self.a.rate
        try:
            while True:
                now = time.time()
                if (now >= t_end and len(rows) >= min_rows) or now >= hard_end:
                    break
                rows.append(self.current())
                dt = period - (time.time() - now)
                if dt > 0:
                    time.sleep(dt)
        finally:
            self.stim.end()                              # stick back to centre even on Ctrl+C
        took = max(time.time() - t0, 1e-3)
        log(f"    снято {len(rows)} отсчётов за {took:.1f} c ({len(rows) / took:.1f} Гц)")
        if not rows:
            return np.zeros((0, len(self.t)), dtype=np.float32)
        return np.vstack(rows)

    @staticmethod
    def _monotonic(du: np.ndarray, tiny: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per column: fraction of moving steps that share the dominant sign, its sign, net travel."""
        nz = np.abs(du) > tiny
        pos = ((du > 0) & nz).sum(axis=0)
        neg = ((du < 0) & nz).sum(axis=0)
        moving = np.maximum(nz.sum(axis=0), 1)
        frac = np.maximum(pos, neg) / moving
        sign = np.where(pos >= neg, 1, -1)
        return frac, sign, np.abs(du.sum(axis=0))

    def phase_c(self) -> List[dict]:
        a, t = self.a, self.t
        log("\n[C] Трассировка: что это за значения")
        log("    Камера движется непрерывно, а я часто перечитываю оставшихся кандидатов, чтобы понять, чем они являются.")
        if len(t) > a.trace_limit:
            self._trim(a.trace_limit, f"лимит --trace-limit {a.trace_limit}")
        if len(t) == 0:
            return []
        self._fit_trace()

        my = self._trace(+1, 0, a.sweep, "Вращайте камеру ВПРАВО с ровной скоростью (желательно больше полного оборота).",
                         min_rows=14)
        mp_up = self._trace(0, +1, a.sweep * 0.5, "Ведите камеру ВВЕРХ до упора и держите.", min_rows=6)
        mp_dn = self._trace(0, -1, a.sweep, "Ведите камеру ВНИЗ до упора и держите.", min_rows=10)
        self._trace(0, +1, a.sweep * 0.35, "Верните камеру ВВЕРХ примерно к горизонту.")
        t.last = self.current()
        if min(len(my), len(mp_up), len(mp_dn)) < 4:
            self.notes.append("traces too short to classify (reading is too slow)")
            log("    !!! Трассы получились слишком короткими: память читается слишком медленно. "
                "Запустите снова с меньшим --trace-limit (например 5000).")
            return []

        n = len(t)
        addrs = t.addrs.astype(np.int64)
        cls = t.cls()
        yawish, pitchish = t.h_ok, t.v_ok        # reacted to every yaw / every pitch step
        index = {int(x): i for i, x in enumerate(addrs.tolist())}
        info: List[dict] = [dict(addr=int(addrs[i]), cls=str(cls[i]), kind="other", score=0.0) for i in range(n)]

        # Samples are NaN where the word was unreadable or not a plausible number.  A column with any
        # NaN cannot be an angle: it is excluded (ok_y / ok_p) and zero-filled so that no arithmetic
        # below ever sees a NaN, an inf or an overflowing value.
        ok_y = np.isfinite(my).all(axis=0)
        both_raw = np.vstack((mp_up, mp_dn))
        ok_p = np.isfinite(both_raw).all(axis=0)
        my = np.where(np.isfinite(my), my, 0.0).astype(np.float32)
        mp_up = np.where(np.isfinite(mp_up), mp_up, 0.0).astype(np.float32)
        mp_dn = np.where(np.isfinite(mp_dn), mp_dn, 0.0).astype(np.float32)
        both = np.vstack((mp_up, mp_dn))
        log(f"    пригодные трассы: yaw {int(ok_y.sum())} из {n}, pitch {int(ok_p.sum())} из {n} "
            f"(остальные содержат нечисловые значения)")

        with np.errstate(all="ignore"):
            # ---- yaw: scalar angles
            absmax = np.abs(my).max(axis=0)
            dy = np.diff(my, axis=0)
            for period, limit, kind, min_travel, tiny in ((TWO_PI, TWO_PI + 0.06, "angle_rad", 0.5, 1e-5),
                                                          (360.0, 360.6, "angle_deg", 30.0, 1e-3)):
                du = dy - period * np.round(dy / period)
                frac, sign, travel = self._monotonic(du, tiny)
                wrapped = (np.abs(dy) > period / 2).any(axis=0)
                ok = ok_y & (frac >= 0.9) & (travel >= min_travel) & (absmax <= limit)
                if kind == "angle_deg":
                    ok &= absmax > TWO_PI + 0.06
                for i in np.flatnonzero(ok):
                    if info[i]["kind"] != "other" or not yawish[i]:
                        continue
                    info[i].update(kind=kind, axis="hor", sign_right=int(sign[i]), wrap=bool(wrapped[i]),
                                   travel=float(travel[i]), lo=float(my[:, i].min()),
                                   hi=float(my[:, i].max()))

            # ---- yaw: sin/cos pairs (and the horizontal part of a direction vector)
            for i in range(n):
                if not ok_y[i] or not yawish[i] or info[i]["kind"] != "other":
                    continue
                for off in (4, 8):
                    j = index.get(int(addrs[i]) + off)
                    if j is None or not ok_y[j] or info[j]["kind"] != "other" or not yawish[j]:
                        continue
                    r2 = my[:, i] ** 2 + my[:, j] ** 2
                    mean = float(np.mean(r2))
                    if not (mean > 1e-3 and float(np.std(r2)) < 0.02 * mean):
                        continue
                    ang = np.arctan2(my[:, i], my[:, j])
                    dang = np.diff(ang)
                    dang -= TWO_PI * np.round(dang / TWO_PI)
                    frac, sign, travel = self._monotonic(dang.reshape(-1, 1), 1e-5)
                    if frac[0] < 0.9 or travel[0] < 0.5:
                        continue
                    kind, radius = "vec2", math.sqrt(mean)
                    extra = {}
                    mid = index.get(int(addrs[i]) + 4) if off == 8 else None
                    if mid is not None and ok_p[i] and ok_p[mid] and ok_p[j]:
                        n3 = both[:, i] ** 2 + both[:, mid] ** 2 + both[:, j] ** 2
                        m3 = float(np.mean(n3))
                        if m3 > 1e-3 and float(np.std(n3)) < 0.02 * m3 and float(np.ptp(both[:, mid])) > 0.05:
                            kind, radius = "vec3", math.sqrt(m3)
                            _f, s_mid, _t = self._monotonic(np.diff(mp_up[:, [mid]], axis=0), 1e-5)
                            extra = {"y_addr": int(addrs[mid]), "sign_up": int(s_mid[0])}
                            info[mid].update(kind="vec3_part", axis="vert")
                    # Angle conventions follow KAMI's cameras.  vec2 (HVecVACamera): atan2(word at addr,
                    # word at addr+partner).  vec3 (HVVecCamera): atan2(Z, X) with X at addr, Z at addr+8,
                    # which runs the opposite way round, hence the flipped sign.
                    s_right = int(sign[0]) if kind == "vec2" else -int(sign[0])
                    info[i].update(kind=kind, axis="hor", partner_offset=off, radius=radius,
                                   sign_right=s_right, travel=float(travel[0]), wrap=False, **extra)
                    info[j].update(kind=kind + "_part", axis="hor")
                    break

            # ---- pitch
            pmax_abs = np.abs(both).max(axis=0)
            d_up = np.diff(mp_up, axis=0)
            d_dn = np.diff(mp_dn, axis=0)
            f_up, s_up, tr_up = self._monotonic(d_up, 1e-5)
            f_dn, s_dn, tr_dn = self._monotonic(d_dn, 1e-5)
            for i in range(n):
                if not ok_p[i] or not pitchish[i] or info[i]["kind"] != "other":
                    continue
                if not (f_up[i] >= 0.9 and f_dn[i] >= 0.9 and s_up[i] == -s_dn[i]):
                    continue
                if tr_dn[i] < 1e-3:
                    continue
                if pmax_abs[i] <= 1.75:
                    kind = "angle_rad"
                elif pmax_abs[i] <= 95.0:
                    kind = "angle_deg"
                else:
                    continue
                col = both[:, i]
                hi, lo = float(col.max()), float(col.min())
                # sitting on the extreme for a while with the stick still held = a clamp
                tol = max(1e-4, 1e-3 * (hi - lo))
                sat_hi = int((np.abs(col - hi) <= tol).sum()) >= 5
                sat_lo = int((np.abs(col - lo) <= tol).sum()) >= 5
                info[i].update(kind=kind, axis="vert", sign_up=int(s_up[i]), lo=lo, hi=hi,
                               clamp_lo=sat_lo, clamp_hi=sat_hi, travel=float(tr_dn[i]))

        for i in range(n):
            v = float(t.last[i])
            info[i]["value"] = v if math.isfinite(v) else None
            info[i]["walk"] = bool(t.walk[i])
            info[i]["votes_right"] = int(t.right_votes[i])
            info[i]["votes_up"] = int(t.up_votes[i])
        for addr in self.watch:
            i = index.get(addr)
            if i is None:
                log(f"    [debug 0x{addr:08X}] трассировка: НЕ в списке кандидатов")
                continue
            col = my[:, i]
            log(f"    [debug 0x{addr:08X}] трассировка: {info[i]}\n"
                f"        yaw-трасса: пригодна={bool(ok_y[i])} n={len(col)} min={col.min():.4f} max={col.max():.4f} "
                f"первые={np.round(col[:6], 4).tolist()} последние={np.round(col[-4:], 4).tolist()}")
        n_hor = sum(1 for c in info if c.get("axis") == "hor" and c["kind"] in CAM_KINDS)
        n_vert = sum(1 for c in info if c.get("axis") == "vert" and c["kind"] in CAM_KINDS)
        log(f"    похожи на угол/вектор: горизонталь {n_hor}, вертикаль {n_vert}")
        if n_hor == 0 and n_vert == 0:
            self.notes.append("no candidate looks like an angle or direction vector")
        return info

    # -- phase D: write test ----------------------------------------------
    def wait_still(self, idx: np.ndarray, timeout: float) -> bool:
        """Wait (at most ``timeout`` s) until none of the rows ``idx`` moves any more, i.e. the game
        has finished smoothing.  Only the classified candidates are watched: among tens of thousands
        of raw survivors there is always one that moves, which used to make this wait run into its
        timeout on every single candidate."""
        if len(idx) == 0:
            time.sleep(min(timeout, 0.2))
            return True
        end = time.time() + timeout
        prev = self.current(idx)
        while time.time() < end:
            time.sleep(0.08)
            cur = self.current(idx)
            with np.errstate(all="ignore"):
                moving = int((np.abs(cur - prev) > self.a.eps_same).sum())
            prev = cur
            if moving == 0:
                return True
        return False

    def _probe_setup(self, info: List[dict]) -> Probe:
        t, a = self.t, self.a
        is_cls = np.array([c["kind"] in PROBE_KINDS for c in info], dtype=bool)
        cls_idx = np.flatnonzero(is_cls)
        others = np.flatnonzero(~is_cls)
        if len(others) > a.poke_probe:
            samp = others[np.linspace(0, len(others) - 1, a.poke_probe).astype(np.int64)]
        else:
            samp = others
        idx = np.concatenate((cls_idx, samp)).astype(np.int64)
        noisy = np.zeros(len(idx), dtype=bool)
        if len(idx):
            # whatever moves on its own now (idle animation, timers) must not pass for "influence"
            base = self.current(idx)
            for _ in range(3):
                time.sleep(0.12)
                cur = self.current(idx)
                with np.errstate(all="ignore"):
                    noisy |= ~np.isfinite(cur) | (np.abs(cur - base) > a.eps_move)
        k = len(cls_idx)
        weight = len(others) / max(1, len(samp))
        return Probe(cls_idx[~noisy[:k]], samp[~noisy[k:]], weight, int(noisy.sum()))

    def _put(self, w_addr: int, value: float) -> Optional[Tuple[str, str]]:
        """Write one float and read it straight back.  None = it landed; otherwise ("error", text)
        when the write or the read-back failed, or ("lost", text) when memory holds another value a
        moment after the write (the game rewrote a derived copy at once - or the write went nowhere)."""
        raw = struct.pack(">f", value)
        try:
            self.mem.write(w_addr, raw)
        except MemoryError_ as e:
            return "error", f"запись не удалась: {e}"
        for _ in range(3):
            try:
                back = self.mem.read(w_addr, 4)
            except MemoryError_ as e:
                return "error", f"запись выполнена, но проверочное чтение не удалось: {e}"
            if back == raw:
                return None
        return "lost", "сразу после записи в памяти другое значение (игра затёрла запись или она не дошла)"

    def _poke_one(self, c: dict, probe: Probe) -> None:
        a, t = self.a, self.t
        addr = c["addr"]
        idx = self.pos[addr]
        targets = [idx]
        if c["kind"] in ("vec2", "vec3"):
            targets.append(self.pos[addr + c["partner_offset"]])
        all_idx = np.unique(np.concatenate((probe.cls, probe.samp, np.array(targets, dtype=np.int64)))).astype(np.int64)
        where = {int(r): k for k, r in enumerate(all_idx.tolist())}
        before = self.current(all_idx)
        v0 = float(before[where[idx]])
        if not math.isfinite(v0):
            c["poke_error"] = "перед записью значение не является числом"
            return
        if c["kind"] in ("vec2", "vec3"):
            s0, c0 = v0, float(before[where[targets[1]]])
            if not math.isfinite(c0):
                c["poke_error"] = "перед записью значение пары не является числом"
                return
            r = math.hypot(s0, c0)
            ang = math.atan2(s0, c0) + (-0.35 if c.get("flip") else 0.35)
            writes = [(addr, s0, r * math.sin(ang)), (addr + c["partner_offset"], c0, r * math.cos(ang))]
        else:
            step = 0.35 if c["kind"] == "angle_rad" else 20.0
            if c.get("axis") == "vert":
                step = 0.15 if c["kind"] == "angle_rad" else 8.0
            mid = (c.get("lo", v0) + c.get("hi", v0)) / 2
            if v0 > mid:
                step = -step                                # stay inside the range the game uses
            if c.get("flip"):
                step = -step                                # second try, the other way (see phase_d)
            writes = [(addr, v0, v0 + step)]
        c["start"] = round(v0, 6)
        written: List[Tuple[int, float]] = []
        restore = True
        try:
            for w_addr, old, new in writes:
                failed = self._put(w_addr, new)
                if failed is not None:
                    written.append((w_addr, old))        # it may have been half written: put the old one back
                    kind, msg = failed
                    if kind == "error":
                        c["poke_error"], c["write_failed"] = msg, True
                        self.write_stats["write_errors"] += 1
                    else:                                # a derived copy the game rewrites every frame
                        c.update(lost=True, sticky=False, reverted=True, written=round(new, 6), influence=0)
                        self.write_stats["write_lost"] += 1
                    return
                written.append((w_addr, old))
            time.sleep(a.poke_wait)
            after = self.current(all_idx)
            v1 = float(after[where[idx]])
            if not math.isfinite(v1):
                c["poke_error"] = "после записи значение перестало быть числом"
                return
            target, delta = writes[0][2], writes[0][2] - writes[0][1]
            tol = max(2e-3, 0.1 * abs(delta))
            period = {"angle_rad": TWO_PI, "angle_deg": 360.0}.get(c["kind"]) if c.get("axis") == "hor" else None

            def apart(x: float, y: float) -> float:
                d = x - y
                return abs(d - period * round(d / period)) if period else abs(d)

            c["sticky"] = bool(apart(v1, target) <= tol)
            c["reverted"] = bool(apart(v1, v0) <= tol)
            c["written"], c["read_back"] = round(target, 6), round(v1, 6)
            with np.errstate(all="ignore"):
                moved = np.abs(after - before) > a.eps_move          # NaN compares false
            moved[[where[i] for i in targets]] = False
            in_cls = np.isin(all_idx, probe.cls)
            in_samp = np.isin(all_idx, probe.samp)
            c["influence"] = int(moved[in_cls].sum()) + int(round(float(moved[in_samp].sum()) * probe.weight))
            c["influenced"] = [int(x) for x in t.addrs[all_idx[moved]][:64].tolist()]
            restore = not c["reverted"]
        finally:
            if restore:
                for w_addr, old in written:
                    try:
                        self.mem.write(w_addr, struct.pack(">f", old))
                    except MemoryError_:
                        pass
        # let the game finish reacting, or the next test would count this one's echo
        self.wait_still(probe.cls, a.poke_settle)

    @staticmethod
    def _span(c: dict) -> Optional[float]:
        """Range covered in the traces, as a fraction of a full turn (yaw) or in radians (pitch)."""
        if c["kind"] not in ("angle_rad", "angle_deg") or "lo" not in c or "hi" not in c:
            return None
        span = c["hi"] - c["lo"]
        return span if c["kind"] == "angle_rad" else math.radians(span)

    @classmethod
    def _plausible(cls, c: dict) -> bool:
        """False for a value that moved with the camera but covers far too little to be the angle itself
        (a 0..1 blend factor, a tiny wobble).  Pairs and vectors are not judged here."""
        span = cls._span(c)
        if span is None:
            return True
        return span >= (TWO_PI * 0.5 if c.get("axis") == "hor" else 0.15)

    @classmethod
    def _range_bonus(cls, c: dict) -> float:
        span = cls._span(c)
        if span is None:
            return 0.0
        if c.get("axis") == "hor":
            return 1.0 if span >= 0.9 * TWO_PI else (-1.5 if span < 0.5 * TWO_PI else 0.0)
        return 1.0 if span >= 0.5 else (-1.5 if span < 0.15 else 0.0)

    def _rank_for_poke(self, info: List[dict]) -> Dict[str, List[dict]]:
        """Candidates of each axis, most likely master first.  (They used to be tried in address
        order, so a camera object in the heap came after every low-address look-alike and fell
        outside --poke-count.)"""
        out: Dict[str, List[dict]] = {}
        cams = {ax: [c for c in info if c.get("axis") == ax and c["kind"] in CAM_KINDS] for ax in ("hor", "vert")}
        near = {ax: np.sort(np.array([c["addr"] for c in lst], dtype=np.int64)) for ax, lst in cams.items()}

        def dist_to(other: str, addr: int) -> int:
            arr = near[other]
            if len(arr) == 0:
                return 1 << 30
            k = int(np.searchsorted(arr, addr))
            return min(abs(int(arr[j]) - addr) for j in (k - 1, k) if 0 <= j < len(arr))

        for ax, lst in cams.items():
            other = "vert" if ax == "hor" else "hor"
            want = "H" if ax == "hor" else "V"

            def pre(c: dict) -> float:
                # A pair / vector that keeps a constant radius is much stronger evidence of a real angle than
                # a lone number that merely moved monotonically, so the kinds start level (or in favour of
                # pairs) - otherwise look-alike scalars bury the master of a sin/cos or vector camera.
                s = {"angle_rad": 3.0, "angle_deg": 3.0, "vec2": 3.5, "vec3": 4.5}[c["kind"]]
                if c["cls"] == want:
                    s += 1.0
                if c.get("walk"):
                    s -= 1.5
                sgn = c.get("sign_right") if ax == "hor" else c.get("sign_up")
                votes = c.get("votes_right", 0) if ax == "hor" else c.get("votes_up", 0)
                if c["kind"] in ("angle_rad", "angle_deg") and sgn and votes * sgn > 0:
                    s += min(1.0, 0.5 * abs(votes))         # the step phase saw the same direction as the trace
                if ax == "vert":
                    s += 0.5 * (bool(c.get("clamp_lo")) + bool(c.get("clamp_hi")))
                s += self._range_bonus(c)                   # a real yaw sweeps the circle, a real pitch has a range
                if dist_to(other, c["addr"]) < 0x400:
                    s += 1.5                                # yaw and pitch masters live in one object
                return s

            out[ax] = sorted(lst, key=lambda c: (-pre(c), c["addr"]))
        return out

    def phase_d(self, info: List[dict]) -> None:
        a = self.a
        if a.no_poke or getattr(self.mem, "writable", True) is False:
            self.notes.append("write test skipped")
            log("\n[D] Проверка записью пропущена (--no-poke или память открыта только для чтения).")
            return
        ranked = self._rank_for_poke(info)
        n_h, n_v = len(ranked["hor"]), len(ranked["vert"])
        log("\n[D] Проверка записью (камера будет дёргаться - так и должно быть)")
        if n_h + n_v == 0:
            self.notes.append("write test skipped: nothing to test")
            log("    Проверять нечего: на фазе C не нашлось ни одного значения, похожего на угол или вектор.")
            return
        cap = a.poke_count
        plan_h, plan_v = ranked["hor"][:cap], ranked["vert"][:cap]
        for plan_ax in (plan_h, plan_v):
            for r, c in enumerate(plan_ax):
                c["rank"] = r
        plan: List[dict] = []
        for k in range(max(len(plan_h), len(plan_v))):                  # alternate axes: a time-out hurts neither
            if k < len(plan_h):
                plan.append(plan_h[k])
            if k < len(plan_v):
                plan.append(plan_v[k])
        log(f"    Кандидатов: горизонталь {n_h}, вертикаль {n_v}; проверю не больше {len(plan)} "
            f"(по {cap} на ось, лучшие первыми) и не дольше {a.poke_budget:.0f} с.")
        log(f"    Я записываю в адрес новое значение, через {a.poke_wait:.2f} с смотрю, осталось ли оно, и возвращаю старое.")
        log("    Руками ничего делать не нужно, только следите за камерой в игре: если она дёрнулась -")
        log("    запомните адрес из последней строки. Ctrl+C остановит проверку (всё записанное будет возвращено).")
        self.stim.idle(a.settle, "перед проверкой записью")
        self.pos = {int(x): i for i, x in enumerate(self.t.addrs.tolist())}
        probe = self._probe_setup(info)
        log(f"    зонд для подсчёта влияния: {len(probe.cls)} классифицированных + {len(probe.samp)} "
            f"выборочных значений (самодвижущихся исключено: {probe.n_noisy})")
        self.wait_still(probe.cls, a.poke_settle)

        t0 = time.time()
        deadline = t0 + a.poke_budget
        found = {"hor": 0, "vert": 0}              # values that kept what was written, per axis
        after = {"hor": 0, "vert": 0}              # candidates tried since the first one that did
        stats = self.write_stats
        failures_in_row = 0

        def axis_done(ax: str) -> bool:
            return found[ax] >= a.poke_enough or (found[ax] >= 1 and after[ax] >= a.poke_after)

        try:
            for k, c in enumerate(plan, 1):
                axis = c["axis"]
                if axis_done(axis):
                    stats["skipped"] += 1
                    continue
                if time.time() > deadline:
                    left = len(plan) - k + 1
                    self.notes.append(f"write test stopped by the time budget ({a.poke_budget:.0f} s); {left} not tested")
                    log(f"    Время проверки вышло ({a.poke_budget:.0f} с), не проверено ещё {left}. "
                        f"Увеличьте --poke-budget, если нужно больше.")
                    break
                self._poke_one(c, probe)
                stats["tested"] += 1
                # A master that sits next to a limit (or that the game clamps to a value that depends on the
                # situation - e.g. a minimum pitch) swallows a push in that direction: it comes back looking like
                # a derived copy, or lands on the limit.  So try the other way too: always when the value was
                # pulled somewhere else, and for the best-ranked candidates also when it simply came back.
                if (c.get("sticky") is False and not c.get("poke_error") and not c.get("lost")
                        and (not c.get("reverted") or c.get("rank", 10 ** 9) < a.poke_retry)):
                    c["pulled_to"] = c.get("read_back")
                    c["flip"] = True
                    self._poke_one(c, probe)
                err = c.get("poke_error")
                if err:
                    log(f"    [D {k}/{len(plan)}] {axis} 0x{c['addr']:08X} {c['kind']}: ОШИБКА - {err}")
                    failures_in_row = failures_in_row + 1 if c.get("write_failed") else 0
                    if failures_in_row >= 3 and stats["tested"] == failures_in_row:
                        raise WriteUnavailable(err)       # nothing has ever worked: do not grind on
                    continue
                if c.get("lost"):
                    # a single one is just a derived copy; the first three in a row mean the write goes nowhere
                    failures_in_row += 1
                    log(f"    [D {k}/{len(plan)}] {axis} 0x{c['addr']:08X} {c['kind']:9s} {c['written']:+.4f} -> "
                        f"игра затёрла запись мгновенно (производная копия)")
                    if failures_in_row >= 3 and stats["tested"] == failures_in_row:
                        raise WriteUnavailable("сразу после записи в памяти другое значение: запись не доходит до игры")
                    continue
                failures_in_row = 0
                if c["sticky"]:
                    stats["sticky"] += 1
                    if self._plausible(c):
                        found[axis] += 1
                        verdict = "ДЕРЖИТСЯ - похоже на главное значение"
                    else:
                        verdict = ("держится, но размах слишком мал для угла (это не он) - не считаю "
                                   "найденным, проверка продолжается")
                    if c.get("flip"):
                        verdict += f" (со второй попытки, в другую сторону; первая вернулась к {c['pulled_to']})"
                else:
                    if found[axis] >= 1:
                        after[axis] += 1
                    verdict = ("игра вернула старое значение (производная копия)" if c["reverted"]
                               else f"ушло к другому значению ({c['read_back']:.4f})")
                log(f"    [D {k}/{len(plan)}] {axis} 0x{c['addr']:08X} {c['kind']:9s} {c['written']:+.4f} -> "
                    f"{verdict}; влияние {c['influence']}")
                if all(axis_done(ax) for ax, n in (("hor", len(plan_h)), ("vert", len(plan_v))) if n):
                    log("    Держащиеся значения найдены на всех осях (ещё несколько кандидатов после первой находки "
                        "проверено), дальше не проверяю. Больше проверять: --poke-after, --poke-enough.")
                    break
        except WriteUnavailable as e:
            self.notes.append(f"write test aborted: writing does not work ({e})")
            log("\n    !!! Запись в память игры не работает - первые три попытки подряд закончились ошибкой:")
            log(f"        {e}")
            log(f"        Возможная причина: {write_error_hint(str(e))}.")
            log("        Проверьте вручную: py -3 watch.py poke 0xАДРЕС 0.1 --add   (адрес любого из кандидатов).")
            log("        Запасной путь: --backend pine (в RPCS3 включён IPC, KAMI закрыт); он медленный.")
        finally:
            stats["seconds"] = round(time.time() - t0, 1)
        log(f"    проверено {stats['tested']} за {stats['seconds']:.0f} с; держатся: {stats['sticky']}; "
            f"ошибок записи: {stats['write_errors']}; затёрты мгновенно: {stats['write_lost']}")
        keepers = [f"{c['axis']} 0x{c['addr']:08X}" for c in plan if c.get("sticky")]
        if keepers:
            log("    Держатся: " + ", ".join(keepers))

    # -- scoring ----------------------------------------------------------
    @staticmethod
    def score(info: List[dict]) -> None:
        for c in info:
            s = {"angle_rad": 3.0, "angle_deg": 3.0, "vec2": 2.5, "vec3": 2.5}.get(c["kind"], 0.0)
            if s == 0.0:
                c["score"] = 0.0
                continue
            want = "H" if c.get("axis") == "hor" else "V"
            if c["cls"] == want:
                s += 1.0
            if c["kind"] == "vec3":
                s += 1.0                       # x/z of a direction vector legitimately react to pitch
            if c["kind"] in ("angle_rad", "angle_deg"):
                # the yaw trace is a sweep of about a full turn or more: the real yaw covers (almost) the
                # whole circle, a value that only wanders through a part of it is something else; likewise a
                # pitch with no range at all
                rb = CamFinder._range_bonus(c)
                s += rb if rb > 0 else rb * (2.0 / 1.5)
            if c.get("sticky"):
                s += 5.0
            elif c.get("reverted") is False:
                s += 1.0                       # neither kept nor reset: pulled toward another value
            s += min(3.0, math.log10(1 + c.get("influence", 0)) * 1.5)
            if c.get("walk"):
                s -= 1.5
            c["score"] = round(s, 2)

    def build_result(self, info: List[dict], t0: float, partial: bool = False) -> dict:
        self.score(info)
        hor = sorted((c for c in info if c.get("axis") == "hor" and c["score"] > 0),
                     key=lambda c: -c["score"])
        vert = sorted((c for c in info if c.get("axis") == "vert" and c["score"] > 0),
                      key=lambda c: -c["score"])
        # masters of one camera normally live in the same object: favour close pairs
        pair = None
        for h in hor[:8]:
            for v in vert[:8]:
                dist = abs(h["addr"] - v["addr"])
                val = h["score"] + v["score"] - (0 if dist < 0x400 else 2.0)
                if pair is None or val > pair[0]:
                    pair = (val, h["addr"], v["addr"], dist)

        def fmt(c):
            d = {k: v for k, v in c.items() if k != "influenced"}
            if any(lo <= c["addr"] < hi for lo, hi in self.static):
                d["static"] = True
            d["addr"] = f"0x{c['addr']:08X}"
            if "y_addr" in d:
                d["y_addr"] = f"0x{d['y_addr']:08X}"
            return d

        res = {
            "tool": "camfind", "elapsed_s": round(time.time() - t0, 1),
            "backend": getattr(self.mem, "name", "?"), "stimulus": self.stim.name,
            "scanned_bytes": self.total, "survivors": len(self.t) if self.t is not None else 0,
            "hor": [fmt(c) for c in hor[:self.a.top]], "vert": [fmt(c) for c in vert[:self.a.top]],
            "best_pair": ({"hor": f"0x{pair[1]:08X}", "vert": f"0x{pair[2]:08X}", "distance": pair[3]}
                          if pair else None),
            "write_test": dict(self.write_stats),
            "notes": list(self.notes),
        }
        if partial or self.interrupted:
            res["partial"] = True
        return res

    def run(self) -> dict:
        t0 = time.time()
        info: List[dict] = []
        try:
            if self.a.known_hor or self.a.known_vert:
                self.seed_known()
            else:
                self.phase_a()
                self.phase_b()
            info = self.phase_c()
            if info and self.checkpoint:
                self.checkpoint(self.build_result(info, t0, partial=True))
                log("    (промежуточный результат фаз A-C сохранён: если дальше что-то пойдёт не так, он останется)")
            if info:
                try:
                    self.phase_d(info)
                except (KeyboardInterrupt, EOFError, NothingLeft):
                    raise
                except Exception as e:                  # a bug here must not cost the result of phases A-C
                    import traceback
                    self.notes.append(f"write test crashed: {e!r}")
                    log("\n!!! Проверка записью упала с ошибкой (результат фаз A-C сохранён, но записью не проверен):")
                    log(traceback.format_exc())
        except NothingLeft as e:
            self.notes.append(f"stopped: {e}")
            log(f"\n!!! Поиск остановлен: {e}.")
            log("    Что делать: встаньте в спокойном месте (без анимации вокруг, без боя), не трогайте стики между")
            log("    шагами, двигайте камеру именно тогда, когда просят. Если не помогает - запустите с большими")
            log("    --eps-same (например 0.01) и --idle-gap 1.5, чтобы шум памяти фильтровался мягче.")
        except (KeyboardInterrupt, EOFError):
            self.interrupted = True
            self.notes.append("interrupted by the user")
            log("\n!!! Прервано (Ctrl+C). Сохраняю то, что успело найтись.")
        return self.build_result(info, t0)


def print_summary(res: dict) -> None:
    def table(title, rows, extra):
        print(f"\n{title}")
        if not rows:
            print("   (ничего не найдено)")
            return
        print("   адрес       тип        класс  липкий  влияние  балл   " + extra)
        for c in rows:
            sticky = {True: "да", False: "нет"}.get(c.get("sticky"), "?")
            value = c.get("value")
            value = 0.0 if value is None else value
            if extra.startswith("вправо"):
                tail = f"{'+' if c.get('sign_right', 0) > 0 else '-'}       {value:9.4f}"
                if c["kind"] in ("vec2", "vec3"):
                    tail += f"  пара +{c['partner_offset']}, r={c.get('radius', 0):.3f}"
            else:
                lim = f"[{c.get('lo', 0):.3f}{'*' if c.get('clamp_lo') else ''}, " \
                      f"{c.get('hi', 0):.3f}{'*' if c.get('clamp_hi') else ''}]"
                tail = f"{'+' if c.get('sign_up', 0) > 0 else '-'}      {value:9.4f}  {lim}"
            print(f"   {c['addr']}  {c['kind']:10s} {c['cls']:5s}  {sticky:6s}  {c.get('influence', 0):7d}  "
                  f"{c['score']:5.2f}  {tail}")

    if res.get("partial"):
        print("\n(результат неполный: поиск был прерван)")
        if not any(c.get("sticky") is not None for c in res["hor"] + res["vert"]):
            print("ВНИМАНИЕ: проверка записью не выполнена - адреса ниже НЕ подтверждены (среди них есть производные "
                  "копии). Запустите camfind заново или проверьте адреса вручную: py -3 watch.py poke 0xАДРЕС 0.5 --add --wait 1.5")
    table("Горизонталь (yaw):", res["hor"], "вправо  значение")
    table("Вертикаль (pitch):", res["vert"], "вверх  значение   пределы (* = упор)")
    unverified = bool(res["hor"]) and res["hor"][0].get("sticky") is not True
    nxt = "Дальше (ВНИМАНИЕ: лучший адрес не подтверждён записью, это может быть производная копия)" if unverified \
        else "Дальше"
    keepers_static = [c["addr"] for c in res["hor"] + res["vert"] if c.get("static") and c.get("sticky")]
    if keepers_static:
        print("\nСтатические адреса среди «липких» (данные самого EBOOT: одни и те же при каждом запуске игры, "
              "цепочка указателей им не нужна): " + ", ".join(keepers_static))
    if res.get("best_pair"):
        p = res["best_pair"]
        print(f"\nЛучшая пара: hor {p['hor']}  vert {p['vert']}  (расстояние 0x{p['distance']:X})")
        if p["distance"] >= 0x400:
            print("ВНИМАНИЕ: эти два адреса лежат в разных объектах, это не пара одной камеры. ptrscan по обоим сразу даст "
                  "бессмысленные цепочки (огромное смещение у одного из полей). Ищите цепочки для осей по отдельности "
                  "или найдите вторую ось рядом с первой: py -3 watch.py struct <адрес> --seconds 10 --changed-only")
            print(f"{nxt}, по осям отдельно: python ptrscan.py find --target hor={p['hor']} -o chains_hor.json   и   "
                  f"python ptrscan.py find --target vert={p['vert']} -o chains_vert.json")
        else:
            print(f"{nxt}: python ptrscan.py find --target hor={p['hor']} --target vert={p['vert']} -o chains.json")
    elif res["hor"]:
        print(f"\n{nxt}: python ptrscan.py find --target hor={res['hor'][0]['addr']} -o chains.json")
    wt = res.get("write_test") or {}
    cands = res["hor"] + res["vert"]
    if wt.get("tested") and not any(c.get("sticky") for c in cands):
        if wt.get("write_errors"):
            print("\nЗапись в память игры выдаёт ошибки, поэтому проверка записью ничего не подтвердила.")
            print("  1. Запустите терминал от имени администратора (особенно если rpcs3 запущен так же).")
            print("  2. Проверьте запись на одном адресе: py -3 watch.py poke 0xАДРЕС 0.1 --add --wait 1")
            print("  3. Запасной путь: --backend pine (IPC в RPCS3 включён, KAMI закрыт); медленно.")
        else:
            print(f"\nНи одно из {wt['tested']} проверенных значений не удержало записанное: камеру через них двигать нельзя.")
            print("  1. Вручную проверьте лучшего кандидата (камера должна заметно повернуться и остаться):")
            if cands:
                print(f"       py -3 watch.py poke {cands[0]['addr']} 0.5 --add --wait 1.5")
            print("  2. Проверьте больше кандидатов: --poke-count 1000 --poke-budget 900 --poke-after 100.")
            print("  3. Если игра перезаписывает угол каждый кадр, а камера стоит на месте, смотрите «Статический анализ»")
            print("     в README (ps3elf.py fieldxref / patch): значение нужно искать не в памяти, а в коде, который его пишет.")
    elif not cands:
        print("\nНичего не найдено. Что попробовать:")
        print("  - повторите в более спокойном месте, двигая камеру именно тогда, когда просят;")
        print("  - --rounds 2 даёт больше шагов отбора; --eps-move 0.0005 ловит более мелкие повороты;")
        print("  - --debug-addr 0xАДРЕС покажет, на каком шаге отсеялся конкретный адрес.")
    for n in res["notes"]:
        print(f"примечание: {n}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_backend_args(ap)
    ap.add_argument("--stimulus", choices=("manual", "timed", "vigem", "sim"), default="manual",
                    help="manual: Enter in the console after every step; timed: no Enter, the scan runs on a "
                         "timer and beeps (stay in the game, e.g. keep the aim button held); vigem: virtual pad")
    ap.add_argument("--prompt-time", type=float, default=6.0, help="timed: seconds to do each requested move")
    ap.add_argument("--lead", type=float, default=2.5, help="timed: seconds to get ready before a trace")
    ap.add_argument("--start-delay", type=float, default=10.0,
                    help="timed: seconds before the scan starts, to switch to the game and e.g. hold the aim button")
    ap.add_argument("--sim-port", type=int, default=28500, help=argparse.SUPPRESS)
    ap.add_argument("--deflection", type=float, default=0.6, help="stick deflection for automatic stimuli")
    ap.add_argument("--range", type=parse_range, default=(0x10000, 0xC0000000), metavar="LO-HI",
                    help="guest address range to scan (default 0x10000-0xC0000000)")
    ap.add_argument("--step-time", type=float, default=0.5, help="seconds of stick deflection per step (automatic)")
    ap.add_argument("--settle", type=float, default=1.0, help="seconds to wait for the camera to come to rest")
    ap.add_argument("--idle-gap", type=float, default=0.7, help="seconds between the two idle snapshots")
    ap.add_argument("--sweep", type=float, default=4.0, help="seconds of the yaw trace")
    ap.add_argument("--rate", type=float, default=30.0, help="trace sampling rate, Hz")
    ap.add_argument("--rounds", type=int, default=None,
                    help="yaw/pitch rounds in phase B (default: 1 by hand, 2 with an automatic stimulus)")
    ap.add_argument("--enough", type=int, default=300, help="stop phase B early below this many candidates")
    ap.add_argument("--eps-move", type=float, default=1e-3, help="|delta| above this counts as changed")
    ap.add_argument("--eps-same", type=float, default=1e-4, help="|delta| below this counts as unchanged")
    ap.add_argument("--max-candidates", type=int, default=5_000_000)
    ap.add_argument("--trace-limit", type=int, default=30000, help="most candidates to trace")
    ap.add_argument("--trace-ms", type=float, default=250.0,
                    help="cut the traced candidates (best first) when one read of them takes longer than this, ms")
    ap.add_argument("--trace-min", type=int, default=3000, help="never cut below this many for --trace-ms")
    ap.add_argument("--poke-count", type=int, default=400, help="candidates per axis to write-test (best first)")
    ap.add_argument("--poke-budget", type=float, default=300.0, help="time limit of the whole write test, seconds")
    ap.add_argument("--poke-enough", type=int, default=3,
                    help="stop testing an axis after this many values kept what was written")
    ap.add_argument("--poke-after", type=int, default=15,
                    help="after the first value that kept what was written, test at most this many more on that axis")
    ap.add_argument("--poke-retry", type=int, default=30,
                    help="the best-ranked candidates per axis that get a second write test in the opposite direction "
                         "when the first one did not hold")
    ap.add_argument("--poke-probe", type=int, default=4000,
                    help="unclassified candidates sampled for the 'influence' count (all of them are too slow)")
    ap.add_argument("--poke-wait", type=float, default=0.25, help="seconds between write and read-back")
    ap.add_argument("--poke-settle", type=float, default=0.8,
                    help="most seconds to wait after a test for the game to settle")
    ap.add_argument("--known-hor", type=parse_int, metavar="ADDR", action="append",
                    help="skip the search: the horizontal value (angle, or the first word of a sin/cos pair or "
                         "direction vector) is at this guest address; only measure it (phases C, D). "
                         "Repeatable: several candidates are measured and write-tested together")
    ap.add_argument("--known-vert", type=parse_int, metavar="ADDR", action="append",
                    help="same for the vertical value; may be combined with --known-hor; repeatable")
    ap.add_argument("--no-poke", action="store_true", help="never write to game memory")
    ap.add_argument("--no-walk", action="store_true", help="skip the walk-forward step")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--debug-addr", type=parse_int, action="append", metavar="ADDR",
                    help="report what happens to this address at every stage (repeatable)")
    ap.add_argument("-o", "--output", default="camfind.json")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.rounds is None:
        args.rounds = 1 if args.stimulus in ("manual", "timed") else 2
    mem = open_backend(args, need_write=not args.no_poke)
    if hasattr(mem, "probe_lo"):                     # PINE has to probe the map: only probe what is scanned
        mem.probe_lo, mem.probe_hi = args.range
    stim = make_stimulus(args.stimulus, port=args.sim_port, deflection=args.deflection,
                         prompt=args.prompt_time, lead=args.lead)

    def save(doc: dict) -> None:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, ensure_ascii=False)

    res = None
    try:
        info = mem.game_info() or pine_info(args.pine)
        if info:
            log(f"источник памяти: {mem.name}; игра: {info.get('id', '?')} {info.get('version', '')} "
                f"{info.get('hash', '')}")
        else:
            log(f"источник памяти: {mem.name}; игра не опознана (IPC в RPCS3 выключен или занят другим клиентом) - "
                f"на поиск камеры это не влияет")
        finder = CamFinder(mem, stim, args)
        if hasattr(stim, "warmup"):
            stim.warmup(args.start_delay)
        try:
            from rpcs3mem.scan import static_ranges
            finder.static = static_ranges(mem)
        except Exception:                              # only used to label addresses in the report
            pass
        finder.checkpoint = lambda doc: save({**doc, "game": info})
        log(f"сканируется {finder.total / 1048576:.0f} МиБ в {len(finder.regions)} областях")
        try:
            res = finder.run()
        except (KeyboardInterrupt, EOFError):
            finder.interrupted = True
            res = finder.build_result([], time.time())
        res["game"] = info
    finally:
        stim.close()
        mem.close()
    if res is None:
        return 1
    if not (res["hor"] or res["vert"]) and os.path.exists(args.output):
        print_summary(res)
        print(f"\nничего не найдено; прежний {args.output} НЕ перезаписан пустым результатом")
        return 2
    save(res)
    print_summary(res)
    print(f"\nрезультат записан в {args.output}")
    return 0 if res["hor"] or res["vert"] else 2


if __name__ == "__main__":
    sys.exit(main())
