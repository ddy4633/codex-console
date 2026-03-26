# 使用官方 Python 基础镜像 (使用 slim 版本减小体积)
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # WebUI 默认配置
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=1455 \
    LOG_LEVEL=info \
    DEBUG=0

# 安装系统依赖
# (curl_cffi 等库可能需要编译工具)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        chromium \
        chromium-driver \
        xvfb \
        xauth \
        fonts-liberation \
        libnss3 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libgbm1 \
        libasound2 \
        libxdamage1 \
        libxcomposite1 \
        libxrandr2 \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 暴露端口
EXPOSE 1455

# 启动 WebUI
# Dokploy 运行时当前目录会落到 /，这里使用绝对路径避免找不到入口脚本。
# 直接拉起 Xvfb 并 exec Python，避免 xvfb-run 包装器残留而服务进程未真正启动。
CMD ["/bin/sh", "-lc", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/tmp/xvfb.log 2>&1 & export DISPLAY=:99 && exec python /app/webui.py"]
