# 保持这个版本
FROM python:3.11-slim-bookworm AS final

# 环境变量配置
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ----------------------------------------------------
# 步骤 1: 以 ROOT 身份安装所有系统依赖和 Python 依赖
# ----------------------------------------------------

# 安装 Playwright 运行所需的系统依赖 (修正：添加 libasound2)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Chromium 运行时必需的依赖
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm-dev \
    libgbm-dev \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxtst6 \
    # 解决缺失的共享库错误: libasound.so.2
    libasound2 \
    # 其他常用构建和权限工具
    build-essential \
    sudo && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制 requirements.txt 并安装应用依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ----------------------------------------------------
# 步骤 2: 切换到非 ROOT 用户并安装 Playwright 浏览器
# ----------------------------------------------------

# 安全最佳实践：创建 appuser
RUN useradd --create-home appuser || true 
# 将整个 /app 目录的所有权授予 appuser (由 root 执行)
RUN chown -R appuser:appuser /app

# 复制应用代码 (在 root 用户下复制)
COPY . .

# 关键权限修复：确保 debug 目录存在并属于 appuser
# 注意：我们必须在这里由 root 创建 debug 目录，并授权 appuser 访问
RUN mkdir -p debug && chown appuser:appuser debug

# 现在切换到 appuser
USER appuser

# 以 appuser 身份执行浏览器下载
RUN python -m playwright install chromium

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]