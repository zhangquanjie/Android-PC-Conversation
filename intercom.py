import queue
import json
import os
import time
from pathlib import Path
import shlex
import socket
import struct
import threading
import subprocess
import tempfile
import base64
import wave
import gzip
import logging
import sys
from logging.handlers import RotatingFileHandler
from transport import find_adb, LocalChannel, run_local
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import paramiko
import sounddevice as sd

RATE = 48000
SERIAL = ''


def synthesize(text, target, stop):
    text_path = target.with_suffix('.txt')
    text_path.write_text(text, encoding='utf-8')
    def ps_quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = '''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
 $v=$s.GetInstalledVoices() | Where-Object {$_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh-*'} | Select-Object -First 1
 if (-not $v) { throw 'Chinese voice unavailable. Install a Chinese Windows speech voice.' }
 $s.SelectVoice($v.VoiceInfo.Name)
 $f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Stereo)
 $s.SetOutputToWaveFile(TARGET,$f)
 $s.Speak([IO.File]::ReadAllText(TEXT,[Text.Encoding]::UTF8))
} finally { $s.Dispose() }
'''.replace('TARGET', ps_quote(target)).replace('TEXT', ps_quote(text_path))
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 90
        while process.poll() is None:
            if stop.wait(0.1) or time.monotonic() > deadline:
                process.kill()
                process.wait()
                raise RuntimeError('语音合成已取消或超时')
        if process.returncode:
            raise RuntimeError('语音合成失败，请确认 Windows 已安装中文语音包')
    # tinyplay cannot seek over extended WAV format chunks in a FIFO.
    with wave.open(str(target), 'rb') as source:
        parameters = source.getparams()
        pcm = source.readframes(source.getnframes())
    with wave.open(str(target), 'wb') as output:
        output.setparams(parameters)
        output.writeframes(pcm)


def wav_header():
    return struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 0x7ffffff0,
                       b'WAVE', b'fmt ', 16, 1, 2, RATE, RATE * 4, 4, 16,
                       b'data', 0x7fffffcc)


class Intercom:
    def __init__(self, report):
        self.report = report
        self.stop = threading.Event()
        self.talk = threading.Event()
        self.client = None
        self.direct = False
        self.adb_path = None
        self.root_kind = None
        self.helper = "/data/local/tmp/codex-intercom-run.sh"
        self.player = '/data/local/tmp/codex-audio.jar'
        self.thread = None
        self.serial = SERIAL
        self.audio_ready = False
        self.text_jobs = queue.Queue(maxsize=1)
        self.speaking = threading.Event()
        self.last_level = 0

    def level(self, direction, data):
        now = time.monotonic()
        if now - self.last_level < 0.12:
            return
        self.last_level = now
        values = np.frombuffer(data, dtype='<i2').astype(np.float32)
        peak = float(np.max(np.abs(values))) / 32768 if len(values) else 0
        self.report(('level', direction, min(100, peak * 100)))

    def request_text(self, text):
        if self.speaking.is_set() or not self.text_jobs.empty():
            raise RuntimeError('正在朗读，请等待完成后再发送')
        self.text_jobs.put_nowait(text)

    def read_text(self, text):
        self.speaking.set()
        try:
            self.report('正在合成中文语音…')
            with tempfile.TemporaryDirectory(prefix='android-tts-') as folder:
                target = Path(folder) / 'speech.wav'
                synthesize(text, target, self.stop)
                if self.stop.is_set():
                    return
                self.report('正在发送语音到安卓，请稍候…')
                device_file = '/data/local/tmp/codex-tts.pcm'
                try:
                    with wave.open(str(target), 'rb') as audio:
                        rate = audio.getframerate()
                        pcm = audio.readframes(audio.getnframes())
                    raw = target.with_suffix('.pcm')
                    raw.write_bytes(pcm)
                    self.push_file(raw, device_file)
                    if self.stop.is_set():
                        return
                    self.report('正在朗读 → 安卓喇叭')
                    result = self.command(self.player_script(rate, device_file) + ' 2>&1', timeout=240, cancel=True)
                    logging.info('TTS playback: %s', result.decode(errors='replace'))
                    if b'AUDIO_DONE' not in result:
                        raise RuntimeError('安卓系统播放器未确认完成')
                finally:
                    self.cleanup()
                    self.command('rm -f ' + device_file)
        except Exception as exc:
            logging.exception('Text-to-speech or Android playback failed')
            self.report('朗读失败：' + str(exc))
        finally:
            self.speaking.clear()

    def remote(self, script, timeout=20, cancel=True):
        ch = self.client.get_transport().open_session(timeout=10)
        ch.exec_command(script)
        end = time.monotonic() + timeout
        out, err = bytearray(), bytearray()
        try:
            while True:
                while ch.recv_ready():
                    out.extend(ch.recv(65536))
                while ch.recv_stderr_ready():
                    err.extend(ch.recv_stderr(65536))
                if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                    break
                if cancel and self.stop.is_set():
                    raise RuntimeError('连接已取消')
                if time.monotonic() > end:
                    raise RuntimeError('远程命令超时，请检查主机网络或软件源')
                time.sleep(0.03)
            code = ch.recv_exit_status()
            output = out.decode(errors='replace')
            if code:
                raise RuntimeError((err.decode(errors='replace') + '\n' + output)[-2500:].strip())
            return output
        finally:
            ch.close()

    def ensure_adb(self):
        self.report('正在检测 SSH 主机上的 ADB…')
        found = self.remote('if command -v adb >/dev/null 2>&1; then echo FOUND; fi')
        if 'FOUND' not in found:
            self.report('远程未安装 ADB，正在自动安装，请稍候…')
            self.remote(INSTALL_ADB, timeout=300)
        self.remote('adb version')

    def adb(self, args, timeout=20, cancel=True):
        if self.direct:
            return run_local([self.adb_path, *args], self.stop, timeout, cancel)
        return self.remote(shlex.join(['adb', *args]), timeout=timeout, cancel=cancel)

    def push_file(self, local_file, device_file):
        if self.direct:
            self.adb(['-s', self.serial, 'push', str(local_file), device_file], timeout=60)
            return
        remote_file = '/tmp/codex-intercom-' + os.urandom(8).hex()
        compressed = Path(str(local_file) + '.gz')
        compressed.write_bytes(gzip.compress(Path(local_file).read_bytes()))
        try:
            with self.client.open_sftp() as transfer:
                transfer.get_channel().settimeout(15)
                with compressed.open('rb') as source, transfer.file(remote_file + '.gz', 'wb', bufsize=0) as destination:
                    while not self.stop.is_set():
                        data = source.read(768)
                        if not data:
                            break
                        destination.write(data)
            if self.stop.is_set():
                raise RuntimeError('操作已取消')
            self.remote('gzip -dc ' + shlex.quote(remote_file + '.gz') + ' > ' + shlex.quote(remote_file))
            self.adb(['-s', self.serial, 'push', remote_file, device_file], timeout=60)
        finally:
            compressed.unlink(missing_ok=True)
            self.client.exec_command('rm -f ' + shlex.quote(remote_file) + ' ' + shlex.quote(remote_file + '.gz'), timeout=5)

    def root_args(self, script):
        encoded = base64.b64encode(script.encode('utf-8')).decode('ascii')
        args = ['/system/bin/sh', self.helper, encoded]
        if self.root_kind == 'uid':
            return ['su', '0', *args]
        if self.root_kind == 'magisk':
            return ['su', '-c', shlex.join(args)]
        return args

    def prepare_android(self):
        # Avoid sh -c nested quoting: ADB only receives a script path and a base64 argument.
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'run.sh'
            script.write_text('#!/system/bin/sh\nexport PATH=/system/bin:/system/xbin:/vendor/bin:$PATH\neval "$(printf %s "$1" | /system/bin/base64 -d)"\n', encoding='utf-8', newline='\n')
            self.push_file(script, self.helper)
        player = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'android-helper' / 'audio-helper.jar'
        if not player.is_file():
            raise RuntimeError('缺少 Android 系统播放器，请使用完整新版 EXE')
        self.push_file(player, self.player)
        for kind, args in [('uid', ['su', '0', '/system/bin/id']),
                           ('magisk', ['su', '-c', '/system/bin/id']),
                           ('adbd', ['/system/bin/id'])]:
            try:
                output = self.adb(['-s', self.serial, 'shell', '-T', shlex.join(args)])
                if 'uid=0(' in output:
                    self.root_kind = kind
                    return
            except Exception:
                if self.stop.is_set():
                    raise
        raise RuntimeError('无法获取安卓 root，请允许 ADB shell 获取超级用户权限')

    def command(self, script, timeout=12, cancel=False):
        result = self.adb(['-s', self.serial, 'shell', '-T', shlex.join(self.root_args(script))], timeout, cancel)
        return result.encode('utf-8')

    def stream(self, script, incoming=False):
        if incoming:
            args = ['-s', self.serial, 'exec-out', *self.root_args(script)]
        else:
            args = ['-s', self.serial, 'shell', '-T', shlex.join(self.root_args(script))]
        if self.direct:
            return LocalChannel([self.adb_path, *args])
        ch = self.client.get_transport().open_session(timeout=8)
        ch.settimeout(2)
        ch.exec_command(shlex.join(['adb', *args]))
        return ch

    def cleanup(self):
        # Kill only processes started by this tool, checking the executable first.
        self.command('for n in cap play; do f=/data/local/tmp/codex-ptt-$n.pid; '
                     'if [ -f "$f" ]; then p=$(cat "$f"); '
                     'exe=$(readlink /proc/$p/exe); ok=0; case "$exe" in */tinycap|*/tinyplay) ok=1;; '
                     '*/app_process|*/app_process32|*/app_process64) if tr "\\000" " " < /proc/$p/cmdline | grep -q IntercomAudio; then ok=1; fi;; esac; '
                     'if [ "$ok" = 1 ]; then '
                     'kill "$p" 2>/dev/null; i=0; while kill -0 "$p" 2>/dev/null && [ "$i" -lt 10 ]; do sleep .05; i=$((i+1)); done; '
                     'if [ "$(readlink /proc/$p/exe)" = "$exe" ]; then kill -9 "$p" 2>/dev/null; fi; fi; '
                     'rm -f "$f"; fi; done; rm -f /data/local/tmp/codex-ptt-in; tinymix 12 "MIC OFF" >/dev/null; true')

    def start(self, password, input_device, output_device, host='127.0.0.1', port=22, username='root', serial=SERIAL, direct=False):
        self.direct = direct
        self.client = None
        self.root_kind = None
        if hasattr(self, 'old_play'):
            del self.old_play
        self.stop.clear()
        self.talk.clear()
        self.serial = serial
        self.audio_ready = False
        while not self.text_jobs.empty():
            self.text_jobs.get_nowait()
        self.thread = threading.Thread(target=self.run, args=(password, input_device, output_device, host, port, username), daemon=True)
        self.thread.start()

    def run(self, password, input_device, output_device, host, port, username):
        try:
            self.report('正在连接…')
            if self.direct:
                self.adb_path = find_adb(self.report)
            else:
                self.client = paramiko.SSHClient()
                self.client.load_system_host_keys()
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.client.connect(host, port=port, username=username, password=password,
                                    timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False)
                self.client.get_transport().set_keepalive(15)
                self.ensure_adb()
            self.report('正在连接安卓设备…')
            if ':' in self.serial:
                self.adb(['connect', self.serial], timeout=15)
            if not self.serial:
                lines = self.adb(['devices']).splitlines()
                devices = [line.split()[0] for line in lines if len(line.split()) == 2 and line.split()[1] == 'device']
                if len(devices) != 1:
                    raise RuntimeError('请填写设备 IP:5555 或 USB 序列号；留空时必须只有一台已授权设备')
                self.serial = devices[0]
            state = self.adb(['-s', self.serial, 'get-state'], timeout=12)
            if state.strip() != 'device':
                raise RuntimeError('安卓设备未就绪：' + state)
            self.prepare_android()
            self.command('command -v tinycap; command -v /system/bin/app_process; command -v tinymix')
            self.old_play = self.command('tinymix 11').decode().split('>')[-1].split()[0]
            self.audio_ready = True
            self.cleanup()
            while not self.stop.is_set():
                if not self.text_jobs.empty():
                    self.read_text(self.text_jobs.get_nowait())
                elif self.talk.is_set():
                    self.transmit(input_device)
                else:
                    self.receive(output_device)
        except Exception as exc:
            logging.exception('Intercom session failed')
            self.report('错误：' + str(exc))
        finally:
            if self.audio_ready:
                try:
                    self.cleanup()
                    if hasattr(self, 'old_play'):
                        self.command('tinymix 11 ' + shlex.quote(self.old_play) + ' >/dev/null')
                except Exception:
                    logging.exception('Device cleanup failed')
            if self.client:
                self.client.close()
            self.audio_ready = False
            self.report(None)

    def receive(self, device):
        self.command('tinymix 12 "Main Mic" >/dev/null')
        ch = self.stream('tinycap /proc/self/fd/3 -D 0 -d 0 -c 2 -r 48000 -b 16 -p 240 -n 4 3>&1 >/dev/null & '
                         'p=$!; echo $p > /data/local/tmp/codex-ptt-cap.pid; wait $p', True)
        ch.settimeout(0.25)
        try:
            with sd.RawOutputStream(samplerate=RATE, channels=2, dtype='int16', device=device, latency='low') as audio:
                self.report('正在收听安卓端 · 按住按钮或空格键说话')
                pending = b''
                last_data = time.monotonic()
                while not self.stop.is_set() and not self.talk.is_set() and self.text_jobs.empty():
                    try:
                        data = ch.recv(3840)
                    except socket.timeout:
                        if ch.exit_status_ready():
                            raise RuntimeError('安卓录音结束：' + ch.recv_stderr(4096).decode(errors='replace'))
                        if time.monotonic() - last_data > 5:
                            raise RuntimeError('5 秒没有收到安卓音频，请断开重连并检查设备声卡占用')
                        continue
                    if not data:
                        raise RuntimeError('安卓录音连接已断开')
                    pending += data
                    last_data = time.monotonic()
                    count = len(pending) // 4 * 4
                    if count:
                        # This codec exposes microphone on channel 1 and playback loopback on channel 2.
                        samples = np.frombuffer(pending[:count], dtype='<i2').reshape(-1, 2)
                        audible = np.repeat(samples[:, :1], 2, axis=1).astype('<i2').tobytes()
                        self.level('rx', audible)
                        audio.write(audible)
                        pending = pending[count:]
        finally:
            try:
                self.cleanup()
            finally:
                ch.close()

    def player_script(self, rate, filename=None):
        return ('echo $$ > /data/local/tmp/codex-ptt-play.pid; '
                'CLASSPATH=' + self.player + ' exec /system/bin/app_process /system/bin IntercomAudio '
                + str(rate) + (' ' + filename if filename else ''))

    def transmit(self, device):
        ch = self.stream(self.player_script(RATE))
        try:
            ready = bytearray()
            deadline = time.monotonic() + 6
            while b'AUDIO_READY' not in ready:
                while ch.recv_stderr_ready():
                    ready.extend(ch.recv_stderr(4096))
                if b'AUDIO_READY' in ready:
                    break
                if self.stop.is_set() or ch.exit_status_ready() or time.monotonic() > deadline:
                    raise RuntimeError('安卓系统播放器未就绪：' + ready.decode(errors='replace'))
                time.sleep(.03)
            with sd.RawInputStream(samplerate=RATE, channels=1, dtype='int16', device=device,
                                   blocksize=480, latency='low') as audio:
                self.report('正在讲话 → 安卓喇叭 · 松开恢复收听')
                while self.talk.is_set() and not self.stop.is_set():
                    if ch.exit_status_ready():
                        raise RuntimeError('安卓系统播放已中断：' + ch.recv_stderr(4096).decode(errors='replace'))
                    data, _ = audio.read(480)
                    self.level('tx', data)
                    stereo = np.repeat(np.frombuffer(data, dtype='<i2'), 2).tobytes()
                    # Keep individual SSH packets below the problematic routed MTU.
                    for offset in range(0, len(stereo), 768):
                        ch.sendall(stereo[offset:offset + 768])
            ch.shutdown_write()
            deadline = time.monotonic() + 2
            diagnostic = bytearray()
            while time.monotonic() < deadline and not self.stop.is_set():
                while ch.recv_stderr_ready():
                    diagnostic.extend(ch.recv_stderr(4096))
                if b'AUDIO_DONE' in diagnostic or ch.exit_status_ready():
                    break
                time.sleep(.03)
            logging.info('PTT Android playback: %s', diagnostic.decode(errors='replace'))
        finally:
            try:
                self.cleanup()
            finally:
                ch.close()


INSTALL_ADB = r'''
set -e
if [ "$(id -u)" = 0 ]; then elevate="";
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then elevate="sudo -n";
else echo '自动安装 ADB 需要 root 账号或免密码 sudo 权限' >&2; exit 1; fi
if command -v apt-get >/dev/null 2>&1; then
    $elevate env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=60 update
    $elevate env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=60 install -y adb
elif command -v apk >/dev/null 2>&1; then $elevate apk add android-tools
elif command -v dnf >/dev/null 2>&1; then $elevate dnf install -y android-tools
elif command -v yum >/dev/null 2>&1; then $elevate yum install -y android-tools
elif command -v pacman >/dev/null 2>&1; then $elevate pacman -S --needed --noconfirm android-tools
else echo '不支持此主机的包管理器，请手动安装 adb 后重试' >&2; exit 1; fi
command -v adb >/dev/null 2>&1
'''


def main():
    log_dir = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AndroidIntercom'
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'intercom.log', maxBytes=500000, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s', force=True)
    singleton = socket.socket()
    try:
        singleton.bind(('127.0.0.1', 39871))
        singleton.listen(1)
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo('对讲工具已运行', '请先关闭其他新版对讲窗口，再重新打开。')
        root.destroy()
        return
    root = tk.Tk()
    root.title('安卓实时对讲')
    root.geometry('720x850')
    root.resizable(False, False)
    events = queue.Queue()
    engine = Intercom(events.put)
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='电脑 ↔ 安卓 · 按住说话', font=('Microsoft YaHei UI', 17)).pack(pady=8)
    config_path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AndroidIntercom' / 'settings.json'
    try:
        saved = json.loads(config_path.read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    mode = tk.StringVar(value=saved.get('mode', 'SSH 中转'))
    mode_row = ttk.Frame(frame)
    mode_row.pack(fill='x', pady=4)
    ttk.Label(mode_row, text='连接模式', width=15).pack(side='left')
    mode_box = ttk.Combobox(mode_row, state='readonly', textvariable=mode,
                            values=['SSH 中转', '电脑直连 ADB'], width=30)
    mode_box.pack(side='left')
    fields = {}
    for key, label, default in [('host', 'SSH 地址', '127.0.0.1'), ('port', 'SSH 端口', '22'),
                                ('username', 'SSH 用户名', 'root'), ('serial', 'IP:端口 / USB序列号', SERIAL),
                                ('password', 'SSH 密码', '')]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=15).pack(side='left')
        entry = ttk.Entry(row, show='*' if key == 'password' else '', width=55)
        entry.insert(0, '' if key == 'password' else str(saved.get(key, default)))
        entry.pack(side='left', fill='x', expand=True)
        fields[key] = entry
    password = fields['password']
    def change_mode(event=None):
        for key in ('host', 'port', 'username', 'password'):
            fields[key].config(state='disabled' if mode.get() == '电脑直连 ADB' else 'normal')
    mode_box.bind('<<ComboboxSelected>>', change_mode)
    change_mode()
    ttk.Label(frame, text='直连：支持网络 ADB 或 USB，留空自动选择唯一已授权设备。\nSSH：自动检查并安装远程 ADB；密码不保存。安卓仍需 root 和兼容音频接口。').pack(pady=6)
    devices = sd.query_devices()
    inputs = [i for i, d in enumerate(devices) if d['max_input_channels'] > 0]
    outputs = [i for i, d in enumerate(devices) if d['max_output_channels'] >= 2]
    choices = []
    for label, ids, default in [('麦克风', inputs, sd.default.device[0]), ('扬声器', outputs, sd.default.device[1])]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=9).pack(side='left')
        box = ttk.Combobox(row, state='readonly', values=[f'{i}: {devices[i]["name"]}' for i in ids], width=56)
        box.pack(side='left')
        if ids:
            box.current(ids.index(default) if default in ids else 0)
        choices.append(box)
    status = tk.StringVar(value='选择连接模式后点击连接；建议电脑佩戴耳机')
    ttk.Label(frame, textvariable=status, wraplength=570).pack(pady=10)
    meters = {}
    meter_row = ttk.Frame(frame)
    meter_row.pack(fill='x', pady=2)
    for key, label in [('rx', '安卓声音'), ('tx', '电脑麦克风')]:
        ttk.Label(meter_row, text=label).pack(side='left', padx=4)
        bar = ttk.Progressbar(meter_row, maximum=100, length=190)
        bar.pack(side='left', padx=5)
        meters[key] = bar
    connected = False
    def release(event=None):
        engine.talk.clear()
        talk.config(bg='#2374ab')
    def press(event=None):
        if connected and engine.audio_ready and not engine.speaking.is_set() and engine.text_jobs.empty():
            engine.talk.set()
            talk.config(bg='#bc3a36')
    def toggle():
        nonlocal connected
        if connected:
            release()
            engine.stop.set()
            connect.config(state='disabled', text='正在停止…')
        elif choices[0].get() and choices[1].get():
            settings = {k: v.get().strip() for k, v in fields.items() if k != 'password'}
            settings['mode'] = mode.get()
            try:
                direct = mode.get() == '电脑直连 ADB'
                port = 22 if direct else int(settings['port'])
                if not direct and (not 1 <= port <= 65535 or not settings['host'] or not settings['username']):
                    raise ValueError()
                if settings['serial'].startswith('-'):
                    raise ValueError()
            except ValueError:
                messagebox.showerror('配置错误', '请填写 SSH 地址、用户名和安卓地址，并输入有效端口（1–65535）。')
                return
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                messagebox.showerror('保存失败', str(exc))
                return
            connected = True
            connect.config(text='断开连接')
            engine.start(password.get(), int(choices[0].get().split(':')[0]), int(choices[1].get().split(':')[0]),
                         settings['host'], port, settings['username'], settings['serial'], direct=direct)
    connect = ttk.Button(frame, text='连接并收听', command=toggle)
    connect.pack()
    talk = tk.Button(frame, text='按住说话（空格键）', bg='#2374ab', fg='white', font=('Microsoft YaHei UI', 16), height=2)
    talk.pack(fill='x', pady=10)
    talk.bind('<ButtonPress-1>', press)
    ttk.Label(frame, text='输入文字，让安卓喇叭朗读（中文语音，最多 1000 字）：').pack(anchor='w')
    text_box = tk.Text(frame, height=4, font=('Microsoft YaHei UI', 11), wrap='word')
    text_box.pack(fill='x', pady=5)
    def speak_text():
        if not connected or not engine.audio_ready:
            messagebox.showinfo('尚未连接', '请先连接安卓设备，等待进入收听状态。')
            return
        text = text_box.get('1.0', 'end-1c').strip()
        if not text or len(text) > 1000:
            messagebox.showinfo('请输入文字', '请输入 1–1000 字的朗读内容。')
            return
        release()
        try:
            engine.request_text(text)
        except (RuntimeError, queue.Full) as exc:
            messagebox.showinfo('请稍候', str(exc))
    ttk.Button(frame, text='朗读到安卓喇叭', command=speak_text).pack(pady=4)
    root.bind_all('<ButtonRelease-1>', release)
    root.bind('<KeyPress-space>', lambda e: press() if root.focus_get() not in [*fields.values(), text_box] else None)
    root.bind('<KeyRelease-space>', release)
    # Child-to-child focus changes must not cancel a mouse press on the talk button.
    root.bind('<FocusOut>', lambda e: root.after_idle(lambda: release() if root.focus_displayof() is None else None))
    closing = False
    def close():
        nonlocal closing
        closing = True
        release()
        engine.stop.set()
        status.set('正在停止音频并断开连接…')
    root.protocol('WM_DELETE_WINDOW', close)
    def poll():
        nonlocal connected
        while not events.empty():
            msg = events.get_nowait()
            if isinstance(msg, tuple) and msg[0] == 'level':
                meters[msg[1]]['value'] = msg[2]
                continue
            if msg is None:
                connected = False
                connect.config(state='normal', text='连接并收听')
                if not status.get().startswith('错误'):
                    status.set('已断开，音频采集已停止')
            else:
                logging.info('%s', msg)
                status.set(msg)
        if closing and (not engine.thread or not engine.thread.is_alive()):
            root.destroy()
            return
        root.after(100, poll)
    poll()
    root.mainloop()


if __name__ == '__main__':
    main()
