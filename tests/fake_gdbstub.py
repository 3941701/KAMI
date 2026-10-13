#!/usr/bin/env python3
"""Stand-in for RPCS3's GDB stub, following rpcs3/Emu/GDB.cpp closely enough to exercise
gdbtrace.py: the command/data split, '+' acknowledgements, register layout, and the three
behaviours a client has to know about -
  * 'g' reads the FIRST PPU thread unless a thread was selected with Hg;
  * continuing with the breakpoint still planted at the current PC stops again at once;
  * Z0 answers OK whether or not the breakpoint could be set.
The "game": thread 2 keeps executing three store instructions on a camera object."""
import socket
import struct
import sys
import threading
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 28530
T1, T2 = 0x1000001, 0x1000002
CAMERA = 0x30251720
# pc -> (instruction word, register that holds the object, LR)
PROGRAM = {
    0x00010224: ((52 << 26) | (0 << 21) | (3 << 16) | 0x1A8, 3, 0x00010290),     # stfs f0, 0x1A8(r3)
    0x00010230: ((52 << 26) | (0 << 21) | (3 << 16) | 0x1C4, 3, 0x00010290),     # stfs f0, 0x1C4(r3)
    0x00020100: ((52 << 26) | (1 << 21) | (31 << 16) | 0x1A8, 31, 0x00020300),   # same offset, other object
}
OTHER_OBJECT = 0x30990000
NEVER = 0x00030000                                                              # planted but never executed


def checksum(data: bytes) -> bytes:
    return b"%02x" % (sum(data) & 0xFF)


class Stub:
    def __init__(self, conn):
        self.conn = conn
        self.bps = set()
        self.general = 0                      # ANY_THREAD
        self.selected = T1
        self.paused_by = 0
        self.pc = {T1: 0x00050000, T2: 0}
        self.order = list(PROGRAM)
        self.next = 0
        self.log = []

    def getc(self):
        c = self.conn.recv(1)
        if not c:
            raise ConnectionError
        return c

    def read_cmd(self):
        c = self.getc()
        while c == b"+":
            c = self.getc()
        if c == b"\x03":
            return "\x03", ""
        assert c == b"$", c
        cmd, data, cmd_part = "", "", True
        while True:
            c = self.getc()
            if c == b"#":
                break
            ch = c.decode()
            if cmd_part and ch in ":.;":
                cmd_part = False
            if cmd_part:
                cmd += ch
                if len(cmd) == 1 and ch not in "qv":
                    cmd_part = False
            else:
                data += ch
        self.getc(), self.getc()
        self.conn.sendall(b"+")
        return cmd, data

    def reply(self, text: str):
        raw = text.encode()
        self.conn.sendall(b"$" + raw + b"#" + checksum(raw))
        assert self.getc() == b"+"

    def regs(self, tid):
        gpr = [0] * 32
        pc = self.pc[tid]
        lr = 0
        if tid == T2 and pc in PROGRAM:
            _w, reg, lr = PROGRAM[pc]
            gpr[reg] = CAMERA if pc != 0x00020100 else OTHER_OBJECT
        out = "".join("%016x" % g for g in gpr) + "".join("%016x" % 0 for _ in range(32))
        out += "%016x" % pc + "x" * 16 + "%08x" % 0 + "%016x" % lr + "%016x" % 0 + "x" * 8 + "x" * 8
        return out

    def run_until_stop(self):
        """Thread 2 walks its program; stop at the first planted breakpoint or on interrupt."""
        self.conn.settimeout(0.02)
        try:
            while True:
                if self.pc[T2] in self.bps:              # resumed while still on a breakpoint: hit again
                    self.paused_by = T2
                    return
                pc = self.order[self.next % len(self.order)]
                self.next += 1
                self.pc[T2] = pc
                if pc in self.bps:
                    self.paused_by = T2
                    return
                try:
                    c = self.conn.recv(1)
                    if c == b"\x03":
                        return
                    if not c:
                        raise ConnectionError
                except socket.timeout:
                    pass
        finally:
            self.conn.settimeout(None)

    def serve(self):
        while True:
            cmd, data = self.read_cmd()
            self.log.append((cmd, data))
            if cmd == "qC":
                self.reply("QC%016x" % self.selected)
            elif cmd == "H":
                tid = int(data[1:], 16) if data[1:] != "-1" else 0
                if data[0] == "g":
                    self.general = tid
                self.selected = tid or T1
                self.reply("OK")
            elif cmd == "g":
                self.selected = self.general or T1       # select_thread(general_ops_thread_id)
                self.reply(self.regs(self.selected))
            elif cmd == "m":
                addr, size = (int(x, 16) for x in data.split(","))
                word = PROGRAM.get(addr, (0x60000000,))[0]
                self.reply(struct.pack(">I", word)[:size].hex())
            elif cmd == "Z":
                self.bps.add(int(data.split(",")[1], 16))
                self.reply("OK")
            elif cmd == "z":
                self.bps.discard(int(data.split(",")[1], 16))
                self.reply("OK")
            elif cmd == "vCont":
                assert data[1] in "cs", data
                self.run_until_stop()
                self.selected = self.paused_by or T1     # select_thread(pausedBy)
                self.reply("S05")
            elif cmd == "qSupported":
                self.reply("PacketSize=1200")
            else:
                self.reply("")


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(1)
    print("ready", flush=True)
    conn, _ = srv.accept()
    stub = Stub(conn)
    try:
        stub.serve()
    except (ConnectionError, OSError):
        pass
    print("left planted:", sorted(hex(b) for b in stub.bps), flush=True)


if __name__ == "__main__":
    main()
