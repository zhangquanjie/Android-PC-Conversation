"""Build a standalone Windows executable with official Google platform tools."""
import io
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
tools = root / 'platform-tools'
tools.mkdir(exist_ok=True)
url = 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
with urllib.request.urlopen(url, timeout=60) as response:
    archive = zipfile.ZipFile(io.BytesIO(response.read()))
for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll'):
    (tools / name).write_bytes(archive.read('platform-tools/' + name))
helper = root / 'android-helper'
for name, url in [
    ('android.jar', 'https://repo.maven.apache.org/maven2/com/google/android/android/4.1.1.4/android-4.1.1.4.jar'),
    ('r8.jar', 'https://dl.google.com/dl/android/maven2/com/android/tools/r8/8.3.37/r8-8.3.37.jar'),
]:
    target = helper / name
    if not target.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            target.write_bytes(response.read())
classes = helper / 'classes'
classes.mkdir(exist_ok=True)
subprocess.run(['javac', '-source', '8', '-target', '8', '-cp', str(helper / 'android.jar'),
                '-d', str(classes), str(helper / 'IntercomAudio.java')], check=True)
subprocess.run(['java', '-cp', str(helper / 'r8.jar'), 'com.android.tools.r8.D8', '--lib',
                str(helper / 'android.jar'), '--min-api', '23', '--output', str(helper),
                str(classes / 'IntercomAudio.class')], check=True)
with zipfile.ZipFile(helper / 'audio-helper.jar', 'w', zipfile.ZIP_DEFLATED) as package:
    package.write(helper / 'classes.dex', 'classes.dex')
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
                '--collect-all', 'tkinterdnd2',
                '--name', 'AndroidIntercom', '--add-data', f'{tools};platform-tools',
                '--add-data', f'{helper / "audio-helper.jar"};android-helper',
                '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'),
                '--specpath', str(root), str(root / 'intercom.py')], check=True)
