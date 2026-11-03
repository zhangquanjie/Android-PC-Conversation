# Android PC Conversation

Windows 电脑与 Android 设备的按住说话对讲工具，支持电脑直连 ADB、SSH 中转、多语言文字朗读，以及 MP3/WAV 文件播放。

## 使用

从 [Releases](https://github.com/zhangquanjie/Android-PC-Conversation/releases) 下载 `AndroidIntercom.exe`，无需安装 Python。EXE 内置 Google 官方 ADB。

1. 选择“电脑直连 ADB”或“SSH 中转”。直连支持 `IP:端口`、USB 序列号；仅有一台已授权设备时可留空。
2. SSH 模式填写主机、端口、账号和密码。远程缺少 ADB 时尝试通过 Linux 包管理器安装，需要 root 或免密码 sudo，以及可用软件源。
3. 选择电脑实际使用的麦克风和扬声器，点击连接。
4. 按住蓝色按钮或空格键，等待“正在讲话”后向安卓讲话，松开恢复收听。首次启动播放器需要短暂准备，界面会提示继续按住。建议电脑佩戴耳机。
5. “文字朗读”页选择语言/语音，输入对应语言文字后点击“朗读到安卓喇叭”。使用 Windows 本地语音，不使用云服务。
6. 将一个 MP3/WAV 文件拖入窗口：已连接时自动播放，未连接时先选中文件。也可在“MP3 / WAV 文件播放”页选择文件并点击播放。
7. “停止播放”或“停止朗读”可中止当前传输、合成或播放，停止后恢复收听，不需要断开连接。

文件保持原来的 MP3/WAV 格式传输，由 Android 系统解码，沿用已验证的系统扬声器路径。文件名支持中文、空格及大写扩展名；原文件目录不需要写入权限。SSH 传输显示进度，大文件需要等待。

语言下拉框显示电脑当前可由 Windows System.Speech 使用的离线语音，例如 `zh-CN — Microsoft Huihui Desktop`、`en-US — Microsoft Zira Desktop`。语音选择会记住。可打开 Windows 语音设置安装其他语言，再点击“刷新”；并非所有显示语言或 OneCore 语音都能被 System.Speech 读取。更换语音不自动翻译输入文字。

安卓声音、电脑麦克风的音量条显示收发信号。没有声音时先看对应音量条，并检查所选声卡和设备音量。

## 设备要求和限制

- Android 必须启用 ADB 并授权当前连接，具备 root 权限。
- 录音适配 Rockchip codec 的 `tinycap`、`tinymix`：card 0/device 0，麦克风在第一通道、第二通道为回采。不是通用于所有 Android 设备的录音驱动。
- 播放使用 Android `AudioTrack`，由系统选择扬声器/耳机路由和功放，不再强制 `SPK`。实测设备的物理喇叭接在系统选择的 `HP` 路由；固定 `SPK` 虽有数字回采信号却无法出声。
- 兼容 `su 0`、Magisk `su -c`、root adbd，使用绝对路径 `/system/bin/sh` 和设备端辅助脚本避免嵌套命令引号问题。
- SSH 模式需要主机具备 SFTP、gzip。直连模式不需要 SSH。
- MP3/WAV 文件必须使用 Android 支持的编码且未损坏；不支持加密/DRM 音频。
- USB 需安装设备厂商驱动并确认设备上的 USB 调试授权；USB 实物连接尚未在此项目的测试环境中验证。
- 仅允许一个新版窗口运行。使用新版前请先关闭旧版窗口，避免多个版本争抢同一设备声卡。

## 设置和日志

配置：`%LOCALAPPDATA%\AndroidIntercom\settings.json`；密码只保存在运行内存中。

日志：`%LOCALAPPDATA%\AndroidIntercom\intercom.log`。日志不录制音频。反馈问题时请附错误信息及两条音量指示的情况。

无控制台 EXE 显式设置子进程标准输入，并强制安装文件日志处理器，避免继承失效句柄或日志为空。

已复现并修复 Windows 直连模式的 `Errno 22`：本机 `adb shell` 输入把原始 PCM 中的 `0x1A` 当作结束，安卓播放器提前退出，后续写管道失败。直连音频现在增量编码为 Base64，由安卓端解码，避免二进制被文本输入处理截断；SSH 模式保留原来的二进制传输，文件播放不受影响。

验证包含全部 256 种字节、不规则分块、尾部补齐和 Windows 换行；实机传输 SHA-256 一致。真实麦克风连续发送 6 秒，包含 3372 个 `0x1A`，全部 291360 帧完成播放。

## 重连修复

- 停止状态不再阻止设备清理命令；退出时停止本工具启动的音频进程并释放声卡。
- 重连前清理本工具遗留的 PID/FIFO，等待录音或播放进程退出。
- 按住说话按钮时，窗口内部输入焦点变化不再立即撤销讲话。
- 收听只复制真实麦克风通道，不把 codec 的播放回采通道当作第二个麦克风。
- 发送音频按小块传输，避免当前路由链路上较大 SSH 包的阻塞；收听没有数据时明确报错。
- 讲话前等待系统播放器的 `AUDIO_READY`，完成后记录实际播放帧数。文字朗读也会核对系统播放器的完成确认。

在 Ubuntu 26.04 + Magisk 设备上进行了连续两轮连接、收听、讲话、恢复收听和关闭测试，并检查声卡释放。系统播放器测试语音已由现场使用者确认听到。讲话与文字朗读都已验证实际播放帧数完成。

## 源码运行与打包

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python intercom.py
python build.py
```

打包输出 `dist/AndroidIntercom.exe`，构建需要 JDK 17。源码模式缺少可用 ADB 时会下载官方工具到当前用户目录。构建脚本从 Google 下载 platform-tools、R8，从 Maven Central 下载 Android 编译接口，编译 `android-helper/IntercomAudio.java` 为 DEX 并内置播放器。每次 main 分支更新会测试、构建并发布 Windows EXE。

源码首次运行前请执行 `python build.py` 生成 `android-helper/audio-helper.jar`，之后可以直接 `python intercom.py`。

GitHub Actions 的自动化测试覆盖停止后的清理、任务取消、语言参数和中文/空格文件路径处理，不替代设备现场测试。已在目标 Android 上验证 WAV、MP3、英文 Zira/David 语音，以及停止文件播放后恢复收听。拖放回调和中文空格路径也已验证。
