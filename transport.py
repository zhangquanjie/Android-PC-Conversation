import os
import base64
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path


def find_adb(report):
    bundle = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'platform-tools' / 'adb.exe'
    if bundle.is_file():
        return str(bundle)
    cache = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AndroidIntercom' / 'platform-tools' / 'adb.exe'
    if cache.is_file():
        return str(cache)
    existing = shutil.which('adb')
    if existing:
        result = subprocess.run([existing, 'version'], stdin=subprocess.DEVNULL, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=8)
        if b'1.0.32' not in result.stdout and result.returncode == 0:
            return existing
    report('正在下载并安装 Google 官方 ADB 到当前用户目录…')
    cache.parent.parent.mkdir(parents=True, exist_ok=True)
    archive = cache.parent.parent / 'platform-tools.zip'
    with urllib.request.urlopen('https://dl.google.com/android/repository/platform-tools-latest-windows.zip', timeout=30) as response, archive.open('wb') as target:
        shutil.copyfileobj(response, target)
    with zipfile.ZipFile(archive) as package:
        for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll'):
            cache.parent.mkdir(parents=True, exist_ok=True)
            (cache.parent / name).write_bytes(package.read('platform-tools/' + name))
    archive.unlink(missing_ok=True)
    return str(cache)


class Base64Stdin:
    """Incremental encoding; padding is emitted only at EOF, not between PCM blocks."""
    def __init__(self):
        self.pending = b''

    def encode(self, data):
        combined = self.pending + bytes(data)
        size = len(combined) // 3 * 3
        self.pending = combined[size:]
        return base64.b64encode(combined[:size]) + b'\n' if size else b''

    def finish(self):
        result = base64.b64encode(self.pending) + b'\n' if self.pending else b''
        self.pending = b''
        return result


class LocalChannel:
    def __init__(self, args, encode_stdin=False):
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
        self.output = queue.Queue()
        self.errors = bytearray()
        self.pending = bytearray()
        self.timeout = 2
        self.eof = False
        self.encoder = Base64Stdin() if encode_stdin else None
        def reader():
            try:
                while True:
                    data = self.process.stdout.read(3840)
                    if not data:
                        break
                    self.output.put(data)
            finally:
                self.output.put(None)
        def errors():
            for data in iter(lambda: self.process.stderr.read(1024), b''):
                self.errors.extend(data)
        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=errors, daemon=True).start()

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, count):
        if not self.pending and not self.eof:
            try:
                data = self.output.get(timeout=self.timeout)
            except queue.Empty:
                raise socket.timeout()
            if data is None:
                self.eof = True
            else:
                self.pending.extend(data)
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def recv_stderr(self, count):
        result = bytes(self.errors[:count])
        del self.errors[:len(result)]
        return result

    def recv_stderr_ready(self):
        return bool(self.errors)

    def recv_ready(self):
        return bool(self.pending) or not self.output.empty()

    def exit_status_ready(self):
        return self.process.poll() is not None

    def sendall(self, data):
        if self.encoder:
            data = self.encoder.encode(data)
        self._write_all(data)

    def _write_all(self, data):
        view = memoryview(data)
        try:
            while view:
                written = self.process.stdin.write(view)
                if not written:
                    raise RuntimeError('本机 ADB 音频连接已关闭')
                view = view[written:]
        except OSError as exc:
            detail = bytes(self.errors).decode('utf-8', errors='replace')[-1500:]
            raise RuntimeError('本机 ADB 音频输入管道已关闭：' + detail + ' (' + str(exc) + ')') from exc

    def shutdown_write(self):
        if self.encoder:
            self._write_all(self.encoder.finish())
        self.process.stdin.close()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def run_local(args, stop, timeout=20, cancel=True):
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = process.communicate(timeout=0.15)
            break
        except subprocess.TimeoutExpired:
            if (cancel and stop.is_set()) or time.monotonic() > deadline:
                process.kill()
                process.communicate()
                raise RuntimeError('本机 ADB 操作已取消或超时')
    if process.returncode:
        raise RuntimeError((err + out).decode('utf-8', errors='replace')[-2500:])
    return out.decode('utf-8', errors='replace')
