ARG BASE_IMAGE=chrome-driverless-base:latest
FROM ${BASE_IMAGE}

# 与 base 的 python playwright 版本对齐（避免浏览器版本漂移，共用 /root/.cache/ms-playwright 的 chromium）
# playwright 版本已固定死（package.json + package-lock.json），npm ci 严格按 lock 安装
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

COPY main.py .
COPY static/ static/
COPY scripts/ scripts/

# DevTools 反代需要 websockets（base 未装；清华源直连）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple websockets

# 内置浏览器声音：pulseaudio 虚拟声卡（null sink）+ parec 采集 + ffmpeg 转 mp3 流
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 虚拟声卡配置：null sink + monitor 采集源
RUN mkdir -p /tmp/pulse && printf '%s\n' \
    'load-module module-null-sink sink_name=vsink sink_properties=device.description=VirtualSink' \
    'set-default-sink vsink' \
    'load-module module-native-protocol-unix socket=/tmp/pulse/native auth-cookie=/tmp/pulse/cookie' \
    > /tmp/pulse.pa

# base 已装好 chromium-1234（PLAYWRIGHT_BROWSERS_PATH 复用），npm 不再重复下载
RUN cd scripts && npm ci --registry=https://registry.npmmirror.com

EXPOSE 9223 9222

CMD ["sh", "-c", "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99; Xvfb :99 -screen 0 1440x900x24 & sleep 1 && DISPLAY=:99 uvicorn main:app --host 0.0.0.0 --port 9223"]
