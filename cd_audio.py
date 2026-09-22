"""内置浏览器声音：pulse 虚拟声卡 → parec 采集 → ffmpeg mp3 → /audio.mp3 广播。

- 容器/裸机均可：pulse 启动脚本自举（/tmp/pulse.pa 缺失时自动生成）
- null-sink 需要系统 D-Bus，启动前先确保 dbus-daemon 在跑
- parec/ffmpeg 管线异常退出后自动重启；/audio.mp3 为懒启动（首个播放器触发）
"""
import asyncio, os, subprocess, time

from fastapi.responses import StreamingResponse

from cd_app import app
from cd_state import _add_log

audio_clients = set()  # 每个 UI 播放器一个 asyncio.Queue
PULSE_NATIVE = "/tmp/pulse/native"


async def _ensure_pulse():
    """确保 pulse 守护进程已运行（root 运行的警告无害），返回 native socket 是否就绪。"""
    os.makedirs("/tmp/pulse", exist_ok=True)
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/pulse")
    os.environ.setdefault("PULSE_SERVER", "unix:" + PULSE_NATIVE)
    # pulse 启动脚本自举（容器里镜像已烘焙，本地裸跑时自动生成）
    pa = "/tmp/pulse.pa"
    if not os.path.exists(pa):
        with open(pa, "w") as f:
            f.write(
                "load-module module-null-sink sink_name=vsink sink_properties=device.description=VirtualSink\n"
                "set-default-sink vsink\n"
                f"load-module module-native-protocol-unix socket={PULSE_NATIVE} auth-cookie=/tmp/pulse/cookie\n")
    if os.path.exists(PULSE_NATIVE):
        return True
    for attempt in range(3):
        # 注意：--start 守护化在 root 下会静默失败；前台子进程方式稳定常驻
        subprocess.Popen(["rm", "-f", "/tmp/pulse/pulse/pid"])
        try:
            # null-sink 需要系统 D-Bus（缺失会导致 pulse 反复报错退出）
            subprocess.Popen(["sh", "-c", "mkdir -p /run/dbus && (dbus-uuidgen --ensure 2>/dev/null; dbus-daemon --system --fork 2>/dev/null) || true"])
            time.sleep(0.5)
        except Exception:
            pass
        try:
            with open("/tmp/pulse-err.log", "ab") as errf:
                subprocess.Popen(
                    ["pulseaudio", "-nF", "/tmp/pulse.pa", "--exit-idle-time=-1"],
                    stdout=errf, stderr=errf)
        except Exception:
            pass
        for _ in range(25):
            if os.path.exists(PULSE_NATIVE):
                return True
            await asyncio.sleep(0.2)
        await _add_log("WARN", f"[Audio] pulse socket 未就绪 (attempt={attempt + 1})")
    return False


async def _audio_pipeline():
    """把 vsink.monitor 的声音编码成 mp3 广播给所有 /audio.mp3 客户端（带自动重启）。"""
    while not await _ensure_pulse():
        await _add_log("WARN", "[Audio] pulse 不可用，5s 后重试")
        await asyncio.sleep(5)
    await _add_log("INFO", "[Audio] 声音管线就绪 /audio.mp3")
    while True:  # 外层：parec/ffmpeg 异常退出后自动重启
        try:
            parec = await asyncio.create_subprocess_exec(
                "parec", "-d", "vsink.monitor", "--format=s16le", "--rate=44100", "--channels=2",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", "44100", "-ac", "2",
                "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            await _add_log("WARN", "[Audio] parec/ffmpeg 不可用，声音播放禁用")
            return
        await asyncio.gather(_pump(parec.stdout, ffmpeg.stdin), _broadcast(ffmpeg.stdout))
        await _add_log("WARN", "[Audio] 管线中断，3s 后重启")
        await asyncio.sleep(3)


async def _pump(src, dst):
    while True:
        chunk = await src.read(4096)
        if not chunk:
            break
        try:
            dst.write(chunk)
            await dst.drain()
        except Exception:
            break


async def _broadcast(stream):
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        for q in list(audio_clients):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass


@app.get("/audio.mp3")
async def audio_stream():
    if not os.path.exists(PULSE_NATIVE):
        asyncio.create_task(_audio_pipeline())  # 懒启动：首个播放器触发
    q = asyncio.Queue(maxsize=256)
    audio_clients.add(q)

    async def gen():
        try:
            yield b"\xff\xfb\x90\x64"  # mp3 帧头，帮助播放器立即起播
            while True:
                chunk = await q.get()
                yield chunk
        finally:
            audio_clients.discard(q)

    return StreamingResponse(gen(), media_type="audio/mpeg", headers={"Cache-Control": "no-cache"})
