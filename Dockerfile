FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive

# 安装核心依赖
RUN apt-get update && apt-get install -y \
    curl \
    # 下载安装脚本和远程文件
    sudo \
    # 让 dev 用户提权执行管理命令
    wget \
    # 备用的文件下载工具
    git \
    # 版本控制
    jq \
    # JSON 处理工具
    gh \
    # GitHub CLI
    vim \
    # 终端文本编辑器
    net-tools \
    # ifconfig、netstat 等网络诊断命令
    gnupg \
    # 校验软件签名、管理 apt 密钥
    ca-certificates \
    # HTTPS 证书，供 curl 和 apt 访问
    xfce4 \
    # 轻量桌面环境
    xfce4-goodies \
    # XFCE 常用附加组件
    tigervnc-standalone-server \
    # VNC 远程桌面服务
    novnc \
    # 在浏览器里访问 VNC
    websockify \
    # 把 VNC 转成 WebSocket，供 noVNC 使用
    openssh-server \
    # SSH 远程登录
    software-properties-common \
    # 添加和管理第三方 apt 软件源
    build-essential \
    # gcc、make 等编译工具，用来构建原生依赖
    fonts-noto-cjk \
    # 思源黑体等中文字体，桌面和终端能正常显示中文
    ripgrep \
    # rg，按内容快速搜索代码
    zsh \
    # 默认 Shell
    nload \
    # 实时查看网卡流量
    fzf \
    # 模糊查找，Ctrl-R 搜历史、Ctrl-T 搜文件
    zoxide \
    # 按使用频率跳转目录，命令 z / zi
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 安装 uv，作为唯一的 Python 开发工具
RUN curl -LsSf https://astral.sh/uv/install.sh | \
    env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# 安装 Herdr，二进制放到 /usr/local/bin
RUN curl -fsSL https://herdr.dev/install.sh | env HERDR_INSTALL_DIR=/usr/local/bin sh

# 创建 dev 用户，默认 Shell 为 zsh
RUN useradd -m -s /bin/zsh dev && \
    usermod -aG sudo dev && \
    echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# 配置 SSH
RUN sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

# 安装 Node.js、pnpm 和 Playwright
RUN curl -fsSL https://deb.nodesource.com/setup_23.x | bash - \
    && apt-get update && apt-get install -y nodejs \
    && npm install -g pnpm playwright

# 安装 Playwright 的系统依赖和 Chromium
RUN playwright install --with-deps chromium

# 安装 Go 官方二进制，按当前架构下载最新稳定版
RUN arch="$(dpkg --print-architecture)" && \
    version="$(curl -fsSL https://go.dev/VERSION?m=text | head -n 1)" && \
    curl -fsSL "https://go.dev/dl/${version}.linux-${arch}.tar.gz" -o /tmp/go.tgz && \
    rm -rf /usr/local/go && \
    tar -C /usr/local -xzf /tmp/go.tgz && \
    rm /tmp/go.tgz


    
    
# 复制入口脚本和 shell 配置
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY shell/ /opt/cc-runtime/
RUN chmod +x /usr/local/bin/entrypoint.sh /opt/cc-runtime/setup-shell.sh

# 添加系统级别名
RUN echo 'alias yolo="claude --dangerously-skip-permissions"' >> /etc/bash.bashrc

USER dev
WORKDIR /home/dev/workspace
ENV PATH="/usr/local/go/bin:/home/dev/go/bin:/home/dev/.cargo/bin:/home/dev/.local/bin:${PATH}"

# 用户级工具必须在 USER dev 之后安装，否则会落到 /root 下。
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && /home/dev/.local/bin/claude --version

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --no-modify-path --profile minimal --default-toolchain stable \
    && rustc --version && cargo --version

# 安装 zim，并写入 .zshrc / .zimrc
RUN /opt/cc-runtime/setup-shell.sh

# bash 仍保留 Claude Code 的 PATH 和别名
RUN echo 'export PATH="/usr/local/go/bin:$HOME/go/bin:$HOME/.cargo/bin:$HOME/.local/bin:$PATH"' >> /home/dev/.bashrc && \
    echo 'alias yolo="claude --dangerously-skip-permissions"' >> /home/dev/.bashrc

EXPOSE 22 5901 6080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
