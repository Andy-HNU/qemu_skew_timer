#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Timed POSIX FIFO streams for QEMU's native pipe character backend."""
import os
import select
import time


class QemuPipe:
    def __init__(self, path, timeout=30):
        self.timeout = timeout
        self.process = None
        self.buffer = b""
        self.reader = self.writer = None
        try:
            for suffix in (".in", ".out"):
                os.mkfifo(str(path) + suffix, 0o600)
            self.reader = os.open(str(path) + ".out", os.O_RDWR | os.O_NONBLOCK)
            self.writer = os.open(str(path) + ".in", os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            self.close()
            raise

    def read(self, size):
        if self.buffer:
            data, self.buffer = self.buffer[:size], self.buffer[size:]
            return data
        deadline = time.monotonic() + self.timeout
        while not select.select([self.reader], [], [],
                                min(0.1, max(0, deadline - time.monotonic())))[0]:
            # Opening both FIFO ends avoids startup deadlocks but hides EOF.
            if self.process is not None and self.process.poll() is not None:
                if select.select([self.reader], [], [], 0)[0]:
                    break
                raise EOFError(f"QEMU exited with status {self.process.returncode}")
            if time.monotonic() >= deadline:
                raise TimeoutError("QEMU pipe read timed out")
        return os.read(self.reader, size)

    def readline(self):
        data = bytearray()
        while True:
            chunk = self.read(65536)
            if not chunk:
                raise EOFError("QEMU pipe closed")
            line, separator, rest = chunk.partition(b"\n")
            data.extend(line)
            if separator:
                self.buffer = rest + self.buffer
                return bytes(data) + separator

    def write(self, data):
        remaining = memoryview(data)
        deadline = time.monotonic() + self.timeout
        while remaining:
            timeout = max(0, deadline - time.monotonic())
            if not select.select([], [self.writer], [], timeout)[1]:
                raise TimeoutError("QEMU pipe write timed out")
            remaining = remaining[os.write(self.writer, remaining):]

    def close(self):
        for name in ("reader", "writer"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


if __name__ == "__main__":
    from contextlib import closing
    from pathlib import Path
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="skew-pipe-check-") as directory:
        path = Path(directory) / "test"
        with closing(QemuPipe(path, timeout=0.02)) as stream, \
             open(str(path) + ".out", "wb", buffering=0) as peer_write, \
             open(str(path) + ".in", "rb", buffering=0) as peer_read:
            peer_write.write(b"first\nsecond\ntail")
            assert stream.readline() == b"first\n"
            assert stream.readline() == b"second\n"
            assert stream.read(2) == b"ta" and stream.read(2) == b"il"
            stream.write(b"request")
            assert peer_read.read(7) == b"request"
            try:
                stream.read(1)
            except TimeoutError:
                pass
            else:
                raise AssertionError("empty pipe did not time out")
            stream.process = subprocess.Popen([
                sys.executable, "-c",
                "import sys; open(sys.argv[1], 'wb', buffering=0).write(b'last\\n')",
                str(path) + ".out"])
            stream.process.wait(timeout=10)
            assert stream.readline() == b"last\n"
            try:
                stream.read(1)
            except EOFError:
                pass
            else:
                raise AssertionError("process exit was not detected")
        stream.close()
    print("PASS: FIFO buffering, bidirectional I/O, timeout, exit and close")
