# cc-runtime

在一台 Mac 上运行多台 Linux 开发机（`dev-PA`、`dev-CA`），每台有独立身份和出口网关，共用一份通过 Mutagen 同步的工作区。设计见 `docs/architecture.md` 和 `docs/workspace.md`，使用见 `README.md`。

## 常用命令

```bash
uv run python -m unittest discover -s tests -t tests   # 单元测试
uv run cc-runtime status                               # 状态面板（在 Mac 上运行）
```

`cc-runtime` 的大部分命令（`up`、`sync`、`git`、`dedupe`、`schedule`）要在 Mac 上运行：它们操作 Docker、Mutagen、launchd 和 Mac 上的路径。从开发机里经 `ssh -p 10023 <用户>@<网关 IP>` 到 Mac 执行，Mac 上需要先 `export PATH=$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH`。

## 部署约定

- 持久状态优先用宿主机 bind mount，数据直接可见、可备份。例外是 `~/.cache` 所在的外部缓存卷（`.env` 里的 `CACHE_VOLUME` 和各机器的子路径），没有单独要求时不要迁移它。
- 文件按服务归组：Tailscale 配置在 `tailscale/config/`，登录状态在 `tailscale/state/`；每台开发机的家目录、SSH 主机密钥和 machine-id 在 `dev-runtimes/<机器名>/`（`home/`、`ssh-host-keys/`、`machine-id`）。新开发机命名为 `dev-{代号}`。
- 配置和脚本入库；凭据、私钥、运行时状态和家目录不进 Git，也不进 Docker 构建上下文（`.dockerignore` 是白名单，只放行镜像需要的文件）。
- 更换镜像、重建容器或移动存储时，保留 SSH 主机密钥和 Tailscale 状态。移动运行中的状态前先停止对应服务，重启后核对身份（主机密钥指纹、machine-id）。
- 用 `cc-runtime up` 或 `cc-runtime compose` 启动服务。它们会先准备家目录、身份和缓存；直接 `docker compose up` 创建开发机会跳过这一步，新机器会得到全新的身份。
- 开发机之间保持身份独立：不要在家目录之间复制 SSH 主机密钥、machine-id、遥测 ID、`~/.claude` 或 `~/.claude.json`。工具凭据和 SSH 客户端密钥只按 `src/cc_runtime/seed.py` 里的 `CREDENTIALS` 共享，往里加东西前先征得使用者同意。新家目录只用 `cc-runtime seed-home` 初始化。
- 代理配置：先改 `proxy/config.example.yaml`，再把同样的改动应用到每份实际配置（`proxy/config.yaml`、`proxy/config-california.yaml`），然后运行 `cc-runtime check proxy`。模板里不能出现真实凭据或节点/VPS 地址，见 `proxy/README.md`。

## 工作区与 git 数据

- 真实仓库的 `.git` 只通过 `cc-runtime git externalize|restore` 改动，不要手工移动或建链接。含子模块或 `worktree` 的仓库不迁出。
- 同步忽略名单：内置的依赖目录名在 `src/cc_runtime/config.py`；只属于这台 Mac 的条目在 `sync-ignores.local`（不入库）。名单在会话创建时固定，改动要重建会话才生效；重建前确认会话没有冲突，并征得使用者同意。
- 不要同步活的状态：容器以可写方式挂载的、工作区内的路径（比如数据库目录）必须在忽略名单里。改动后运行 `cc-runtime check ignores`。
- 不改动 Mac 侧工作区里的项目内容（去重只替换 Linux 副本里的文件）。

## 迁移期注意（`dev` → `dev-PA` 完成前）

- 运行中的 `cc-runtime-dev-1` 是旧方案的容器，已不在 `compose.yaml` 里，也不能用 compose 重建。不要使用 `--remove-orphans`，也不要用 `--allow-orphans` 启动 `dev-PA`。迁移步骤在 `docs/local/dependency-isolation/stage4-handoff.md`，必须从 Mac 终端或 `dev-CA` 执行，不能在 `dev` 里执行。
- `dev` 直接挂载 Mac 的工作区，仓库里的 `.venv` 和 Mac 共用。在 `dev` 里运行 uv 时设置 `UV_PROJECT_ENVIRONMENT=$HOME/.cache/uv-envs/cc-runtime`，不要覆盖 Mac 的 `.venv`。

## 代码与文档

- Python 代码兼容 3.9（macOS 自带的版本），只依赖标准库和 rich；用户可见的错误抛 `CommandError`，不打印堆栈。改动要有测试；需要 Docker 的测试用 `CC_RUNTIME_DOCKER_TEST=1` 显式开启。
- `docs/` 只写系统现在的设计和取舍。落地过程、验证记录、本机运维细节放在 `docs/local/`（不入库）。
- 文档和提交信息用中文；提交类型用英文（`feat`、`fix`、`docs`、`refactor` 等）。
