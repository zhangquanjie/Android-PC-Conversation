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
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
                '--name', 'AndroidIntercom', '--add-data', f'{tools};platform-tools',
                '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'),
                '--specpath', str(root), str(root / 'intercom.py')], check=True)
