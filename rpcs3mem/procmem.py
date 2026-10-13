"""Direct access to RPCS3 guest memory through the host process.

Why this exists next to the PINE client:

* it is orders of magnitude faster for whole-memory snapshots;
* it does not occupy RPCS3's single IPC client slot, so it works while KAMI
  is connected;
* the host page tables give the list of allocated guest regions for free.

Guest address A lives at host ``base + A`` (the view the game uses) and again
at ``base + 4 GiB + A`` (RPCS3's "sudo" mirror, always read/write).  All
access here goes through the mirror, so pages the emulator has temporarily
protected for reservations are still readable.  ``base`` is normally
0x3_0000_0000 and is auto-detected.

Windows: ReadProcessMemory / WriteProcessMemory / VirtualQueryEx.
Linux:   /proc/<pid>/mem and /proc/<pid>/maps (needs ptrace permission:
         run as root or ``sysctl kernel.yama.ptrace_scope=0``).
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from typing import List, Optional

from .common import (GUEST_SIZE, HOST_BASE_FIRST, HOST_BASE_STEP, MAIN_ELF_BASE, PAGE,
                     SUDO_OFFSET, MemoryError_, Region, merge_regions)

PROCESS_NAMES = ("rpcs3.exe", "rpcs3")


class ProcessError(MemoryError_):
    pass


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
class _WinProcess:
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    PROCESS_QUERY_INFORMATION = 0x0400
    TH32CS_SNAPPROCESS = 0x2
    MEM_COMMIT = 0x1000
    PAGE_NOACCESS = 0x01
    PAGE_READONLY = 0x02
    PAGE_READWRITE = 0x04
    PAGE_WRITECOPY = 0x08
    PAGE_EXECUTE_READ = 0x20
    PAGE_EXECUTE_READWRITE = 0x40
    PAGE_EXECUTE_WRITECOPY = 0x80
    PAGE_GUARD = 0x100

    def __init__(self, pid: Optional[int], writable: bool = True):
        from ctypes import wintypes as wt

        self.wt = wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p),
                        ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wt.DWORD),
                        ("PartitionId", wt.WORD),
                        ("RegionSize", ctypes.c_size_t),
                        ("State", wt.DWORD),
                        ("Protect", wt.DWORD),
                        ("Type", wt.DWORD)]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD),
                        ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wt.DWORD),
                        ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", wt.LONG),
                        ("dwFlags", wt.DWORD),
                        ("szExeFile", wt.WCHAR * 260)]

        self.MBI = MEMORY_BASIC_INFORMATION
        self.PE32 = PROCESSENTRY32W

        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.ReadProcessMemory.restype = wt.BOOL
        k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.WriteProcessMemory.restype = wt.BOOL
        k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.VirtualQueryEx.restype = ctypes.c_size_t
        k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                       ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k32.Process32FirstW.restype = wt.BOOL
        k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wt.BOOL
        k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

        self.pid = pid if pid is not None else self._find_pid()
        access = self.PROCESS_VM_READ | self.PROCESS_QUERY_INFORMATION
        if writable:
            access |= self.PROCESS_VM_WRITE | self.PROCESS_VM_OPERATION
        self.handle = k32.OpenProcess(access, False, self.pid)
        if not self.handle:
            err = ctypes.get_last_error()
            raise ProcessError(
                f"OpenProcess(pid={self.pid}) failed, error {err}. If RPCS3 runs elevated, "
                f"start this tool from an elevated prompt too.")

    def _find_pid(self) -> int:
        k32 = self.k32
        snap = k32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snap or snap == self.wt.HANDLE(-1).value:
            raise ProcessError("CreateToolhelp32Snapshot failed")
        found = []
        try:
            entry = self.PE32()
            entry.dwSize = ctypes.sizeof(self.PE32)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() in PROCESS_NAMES:
                    found.append(entry.th32ProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        if not found:
            raise ProcessError("rpcs3.exe is not running")
        if len(found) > 1:
            raise ProcessError(f"several rpcs3.exe processes found {found}; pass --pid")
        return found[0]

    def read_host(self, host_addr: int, size: int) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        got = ctypes.c_size_t(0)
        ok = self.k32.ReadProcessMemory(self.handle, ctypes.c_void_p(host_addr), buf, size,
                                        ctypes.byref(got))
        if not ok or got.value != size:
            raise ProcessError(f"ReadProcessMemory({host_addr:#x}, {size:#x}) failed, "
                               f"error {ctypes.get_last_error()}")
        return bytes(buf)

    def write_host(self, host_addr: int, data: bytes) -> None:
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        done = ctypes.c_size_t(0)
        ok = self.k32.WriteProcessMemory(self.handle, ctypes.c_void_p(host_addr), buf, len(data),
                                         ctypes.byref(done))
        if not ok or done.value != len(data):
            raise ProcessError(f"WriteProcessMemory({host_addr:#x}) failed, "
                               f"error {ctypes.get_last_error()}")

    def host_regions(self, lo: int, hi: int):
        """Yield (host_start, size, writable, executable) for committed readable pages in [lo, hi)."""
        mbi = self.MBI()
        addr = lo
        readable = (self.PAGE_READONLY | self.PAGE_READWRITE | self.PAGE_WRITECOPY |
                    self.PAGE_EXECUTE_READ | self.PAGE_EXECUTE_READWRITE | self.PAGE_EXECUTE_WRITECOPY)
        writable = self.PAGE_READWRITE | self.PAGE_WRITECOPY | self.PAGE_EXECUTE_READWRITE | \
            self.PAGE_EXECUTE_WRITECOPY
        executable = self.PAGE_EXECUTE_READ | self.PAGE_EXECUTE_READWRITE | self.PAGE_EXECUTE_WRITECOPY
        while addr < hi:
            n = self.k32.VirtualQueryEx(self.handle, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                        ctypes.sizeof(mbi))
            if n == 0:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            if size == 0:
                break
            start, end = max(base, lo), min(base + size, hi)
            if (mbi.State == self.MEM_COMMIT and (mbi.Protect & readable)
                    and not (mbi.Protect & self.PAGE_GUARD) and end > start):
                yield start, end - start, bool(mbi.Protect & writable), bool(mbi.Protect & executable)
            addr = base + size

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.k32.CloseHandle(self.handle)
            self.handle = None


# --------------------------------------------------------------------------
# Linux
# --------------------------------------------------------------------------
class _LinuxProcess:
    _MAPS_RE = re.compile(r"^([0-9a-f]+)-([0-9a-f]+) (\S{4}) ")

    def __init__(self, pid: Optional[int], writable: bool = True):
        self.pid = pid if pid is not None else self._find_pid()
        try:
            self.fd = os.open(f"/proc/{self.pid}/mem", os.O_RDWR if writable else os.O_RDONLY)
        except OSError as e:
            raise ProcessError(
                f"cannot open /proc/{self.pid}/mem: {e}. Run as root or set "
                f"kernel.yama.ptrace_scope=0.") from e

    @staticmethod
    def _find_pid() -> int:
        found = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/comm") as f:
                    comm = f.read().strip().lower()
            except OSError:
                continue
            if comm in PROCESS_NAMES:
                found.append(int(name))
        if not found:
            raise ProcessError("rpcs3 is not running")
        if len(found) > 1:
            raise ProcessError(f"several rpcs3 processes found {found}; pass --pid")
        return found[0]

    def read_host(self, host_addr: int, size: int) -> bytes:
        try:
            data = os.pread(self.fd, size, host_addr)
        except OSError as e:
            raise ProcessError(f"read {host_addr:#x}+{size:#x} failed: {e}") from e
        if len(data) != size:
            raise ProcessError(f"short read at {host_addr:#x}: {len(data)} of {size}")
        return data

    def write_host(self, host_addr: int, data: bytes) -> None:
        try:
            n = os.pwrite(self.fd, data, host_addr)
        except OSError as e:
            raise ProcessError(f"write {host_addr:#x} failed: {e}") from e
        if n != len(data):
            raise ProcessError(f"short write at {host_addr:#x}")

    def host_regions(self, lo: int, hi: int):
        with open(f"/proc/{self.pid}/maps") as f:
            for line in f:
                m = self._MAPS_RE.match(line)
                if not m:
                    continue
                start, end, perms = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
                if end <= lo or start >= hi or "r" not in perms:
                    continue
                start, end = max(start, lo), min(end, hi)
                yield start, end - start, "w" in perms, "x" in perms

    def close(self) -> None:
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None


# --------------------------------------------------------------------------
# Portable front end
# --------------------------------------------------------------------------
class ProcessMemory:
    """Guest-addressed view of a running RPCS3 process."""

    name = "process"

    def __init__(self, pid: Optional[int] = None, base: Optional[int] = None, writable: bool = True):
        if os.name == "nt":
            self.proc = _WinProcess(pid, writable)
        elif sys.platform.startswith("linux"):
            self.proc = _LinuxProcess(pid, writable)
        else:
            raise ProcessError("direct process memory is implemented for Windows and Linux only; "
                               "use the PINE backend")
        self.pid = self.proc.pid
        self.writable = writable
        self.base = base if base is not None else self._detect_base()
        self.sudo = self.base + SUDO_OFFSET

    # -- base detection ---------------------------------------------------
    def _probe_base(self, base: int) -> bool:
        """True when [base] and [base + 4G] mirror each other at the main ELF."""
        try:
            a = self.proc.read_host(base + SUDO_OFFSET + MAIN_ELF_BASE, PAGE)
        except ProcessError:
            return False
        try:
            b = self.proc.read_host(base + MAIN_ELF_BASE, PAGE)
        except ProcessError:
            # the guest view may be momentarily protected; the ELF magic in
            # the mirror is then the only evidence available
            return a[:4] == b"\x7fELF"
        return a == b and any(a)

    def _detect_base(self) -> int:
        for k in range(16):
            cand = HOST_BASE_FIRST + k * HOST_BASE_STEP
            if self._probe_base(cand):
                return cand
        raise ProcessError(
            "could not locate guest memory in the RPCS3 process (is a game running?). "
            "RPCS3.log prints 'vm::g_base_addr = ...' at boot - pass that value with --base.")

    # -- access -----------------------------------------------------------
    def read(self, addr: int, size: int) -> bytes:
        if addr < 0 or addr + size > GUEST_SIZE:
            raise ProcessError(f"guest range {addr:#x}+{size:#x} out of bounds")
        return self.proc.read_host(self.sudo + addr, size)

    def write(self, addr: int, data: bytes) -> None:
        if not self.writable:
            raise ProcessError("backend opened read-only")
        if addr < 0 or addr + len(data) > GUEST_SIZE:
            raise ProcessError(f"guest range {addr:#x}+{len(data):#x} out of bounds")
        self.proc.write_host(self.sudo + addr, data)

    def regions(self) -> List[Region]:
        """Allocated guest regions, from the host mappings of the sudo mirror.

        Protection flags come from the *guest* view where it is mapped, since
        the mirror is read/write for every allocated page.
        """
        mirror = [(s - self.sudo, n) for s, n, _w, _x in
                  self.proc.host_regions(self.sudo, self.sudo + GUEST_SIZE)]
        guest_flags = sorted((s - self.base, n, w, x) for s, n, w, x in
                             self.proc.host_regions(self.base, self.base + GUEST_SIZE))
        out: List[Region] = []
        for start, size in mirror:
            cur, end = start, start + size
            for gs, gn, gw, gx in guest_flags:
                ge = gs + gn
                if ge <= cur or gs >= end:
                    continue
                if gs > cur:                       # hole in the guest view: assume data
                    out.append(Region(cur, gs - cur, True, False))
                seg_end = min(ge, end)
                out.append(Region(max(gs, cur), seg_end - max(gs, cur), gw, gx))
                cur = seg_end
                if cur >= end:
                    break
            if cur < end:
                out.append(Region(cur, end - cur, True, False))
        return merge_regions(out)

    def game_info(self) -> dict:
        return {}

    def close(self) -> None:
        self.proc.close()
