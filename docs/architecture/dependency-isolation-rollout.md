# 依赖隔离落地计划

状态：计划，尚未开始执行（2026-11-12）。设计见 [依赖隔离：双目录同步](dependency-isolation.md)。

## 总体安排

| 阶段 | 内容 | 影响范围 |
| --- | --- | --- |
| 0 | 准备：提交现有改动、定下几个决定 | 只动仓库，不动容器 |
| 1 | 用临时目录验证同步工具，重点是和 git 的配合 | 只动 Mac，不动容器，不碰真实 workspace |
| 2 | `dev-california` 改为 `dev-CA`：目录和服务名 | 只重建这一台，workspace 仍按旧方案 |
| 3 | `dev-CA` 切换到同步的 workspace，并验收 | 只重建 `dev-CA`；`dev` 继续用旧方案 |
| 4 | `dev` 改为 `dev-PA` 并切换，然后清理旧方案 | `dev-CA` 验收全部通过后才开始 |

规则：

- 每个阶段都有验收标准，不通过就停在当前阶段，不进入下一个。
- 阶段 1 是整个方案的闸门。不通过时不是修修补补往下走，而是回到设计。
- 每个阶段开始前要得到使用者的明确同意。
- `dev` 在阶段 4 之前不重建。迁移期间所有 Compose 命令都要写明服务名；不带服务名的 `up` 会重建 `dev`，而不经过 `scripts/dev.py` 的 `up` 还会丢掉它的依赖挂载。

## 当前状态（2026-11-12）

| 项目 | 状态 |
| --- | --- |
| 容器 | `dev`、`dev-california`、两个 proxy、tailscale 都在运行；`dev-california` 是当天新建的 |
| Docker / Compose | 29.4.0 / 5.1.2。已验证 Compose 接受 `dev-CA` 这样含大写字母的服务名 |
| Mutagen | Mac 上已安装 0.18.1，守护进程在运行，没有会话。`.git` 同步的初步实测已做完，临时会话和目录都已删除 |
| Mac 编辑器 | Cursor、Zed、Xcode。Cursor 的后台 `git status` 只读（`GIT_OPTIONAL_LOCKS=0`） |
| `/Users/zeke/.cc-runtime` | 不存在，和 workspace 在同一块磁盘上 |
| 仓库 | 14 个文件有未提交的修改，另有 4 个未跟踪文件 |
| 执行位置 | Docker、Homebrew、同步工具的命令都在 Mac 上执行，从 `dev` 容器经 SSH 发起。这个会话运行在 `dev` 里，所以阶段 4 的重建不能从这里发起 |

## 阶段 0：准备

1. **提交仓库里现有的改动。** 之后每个阶段的修改各自提交，回退时用 Git 就能恢复。
2. **确认以下决定**（括号里是默认做法，没有异议就按它执行）：
   - `machine-id` 放在机器目录下，和 `home/` 同级（是）。
   - `dev-CA` 的 `hostname` 是否从 `dev-california` 改掉（不改）。
   - 本仓库的 `.env` 和代理配置是否从 Linux 副本中排除（不排除，保持现在容器能看到它们的状态）。
   - 是否所有顶层目录都参与同步（是）。

Mutagen 已经装在 Mac 上，不需要安装步骤。

## 阶段 1：验证同步工具

目的：在不碰容器和真实 workspace 的前提下，确认「两个目录 + 同步」对源码和 git 都成立。

准备：在 `~/.cc-runtime/spike/` 下建 `mac/` 和 `linux/` 两个目录，把一个中等规模的真实仓库复制进 `mac/`，按设计文档的参数创建一个临时会话。需要容器参与的检查用一次性容器挂载 `linux/` 来做，用开发镜像，不联网，跑完即删。

| 编号 | 检查 | 通过标准 |
| --- | --- | --- |
| 1.1 | 两个方向的基本同步和延迟 | 单文件改动 2 秒内到达另一侧 |
| 1.2 | 按名字忽略 | 在两侧各建 `node_modules`、`.venv`、`target`，内容都不出现在另一侧 |
| 1.3 | 容器写入能被感知 | 一次性容器写入 `linux/` 的文件，2 秒内出现在 `mac/` |
| 1.4 | 换侧耗时 | 把一个大仓库（例如 `rust_work` 里文件最多的）复制进 `mac/`，两侧交替执行 `git status`，记录换侧后第一次和连续执行的耗时，由使用者判断能否接受 |
| 1.5 | 编辑器开着时容器工作 | Mac 上用 Cursor 打开 `mac/` 里的仓库（保留自动 `fetch`），容器在 `linux/` 里连续修改、提交、切换分支。会话没有冲突，两侧状态一致，`git fsck` 没有错误；记录 Cursor 刷新时的 CPU 占用。Zed 也测一次，并确认它的后台 git 是否写索引 |
| 1.6 | git 写操作 | 一侧提交、切换分支、暂存后，另一侧 `git status` 的结果与之一致，`git fsck` 无错误。两个方向都测 |
| 1.7 | git 同时写 | 两侧在同一秒内各提交一次，会话报告冲突而不是悄悄丢掉一侧；记录恢复步骤 |
| 1.8 | 符号链接 | 相对链接、绝对链接、指向根目录之外的链接都原样到达，会话不停 |
| 1.9 | 删除和重命名 | 一侧删除或重命名一个含有被忽略目录的项目，记录另一侧留下什么 |
| 1.10 | 根目录保护 | 删除 `linux/` 整个目录，会话暂停，`mac/` 的内容没有被删除 |

已知 Mutagen 不保留修改时间，`.git` 随源码同步的正确性已在 Mac 上初步实测（见设计文档「`.git` 的同步」）。1.4 到 1.7 是在真实编辑器、大仓库和容器写入下的复核。不通过时，按设计文档里的备选处理：先试 `.git` 不同步、用链接共用 Mac 侧的 `.git`，再考虑换 Unison 重做 1.1 到 1.10。

阶段结论三选一：

- **通过**：记下最终的参数和使用约定，更新设计文档，进入阶段 2。
- **换做法后通过**：同上，并按采用的备选改写设计文档「`.git` 的同步」及受影响的挂载和工具说明。
- **不通过**：停止。清掉 `~/.cc-runtime/spike/`，回到设计，不进入阶段 2。

结束时无论结果如何都删除临时会话和 `~/.cc-runtime/spike/`。

## 阶段 2：`dev-california` 改为 `dev-CA`

这一步只改目录和名字，workspace 仍按旧方案挂载。和阶段 3 分开做，是为了出问题时能分清是哪一个改动造成的。

步骤：

1. 记录基线：三种 SSH 主机密钥的指纹、`machine-id`、出口 IP、`claude` 的登录状态。
2. 停止并删除 `dev-california-network-init` 和 `dev-california` 两个容器。不先删掉，新服务会因为 IP `172.30.0.11` 被占用而起不来。
3. 把 `dev-california-runtime/` 移为 `dev-runtimes/dev-CA/`。同一文件系统内是改名，不复制内容。
4. 修改引用：

   | 文件 | 改动 |
   | --- | --- |
   | `compose.yaml` | 服务 `dev-california` → `dev-CA`，`dev-california-network-init` → `dev-CA-network-init`（含 `network_mode`、`depends_on`）；三处挂载源改到 `./dev-runtimes/dev-CA/` |
   | `scripts/dev.py` | `DEV_SERVICES` 里的名字；缓存子目录显式保持为现有的 `cache-dev-california`，否则会换成一个空缓存 |
   | `.gitignore`、`.dockerignore` | 增加 `dev-runtimes/` |
   | `tests/`、`scripts/seed-dev-home.py`、`README.md`、`proxy/README.md`、`AGENTS.md` | 名字和路径 |

5. 启动：`python3 scripts/dev.py compose up -d --no-deps dev-CA dev-CA-network-init`。

验收：

- SSH 主机密钥指纹和 `machine-id` 与基线相同；从 Mac 经 2223 端口登录没有主机密钥变更警告。
- `claude` 仍是登录状态，git 凭据可用。
- 出口 IP 仍是加州节点；VNC（5902）和 noVNC（6081）可用。
- `~/.cache` 里原有的内容还在。
- `dev` 容器没有被重建（创建时间不变）。
- `python3 -m unittest discover tests` 通过。

回退：删除新容器，把目录移回原名，用 Git 恢复第 4 步的文件，按原服务名启动。

## 阶段 3：`dev-CA` 切换到同步的 workspace

步骤：

1. 创建 `/Users/zeke/.cc-runtime/linux-workspace`，并把它从 Spotlight 和 Time Machine 中排除。
2. 写入库的两个脚本：创建和查看同步会话的脚本，以及 `scripts/check-sync-ignores.py`。运行检查脚本，处理它报告的误伤和遗漏。
3. 创建会话，等首次同步完成。完成的标准是会话状态里没有冲突和问题，并且两棵树在排除忽略名单后内容一致。
4. 修改配置：

   | 文件 | 改动 |
   | --- | --- |
   | `compose.yaml` | `dev-CA` 的 workspace 挂载源改为 `/Users/zeke/.cc-runtime/linux-workspace`；`~/.cache` 的卷挂载和两个缓存环境变量从生成文件移到这里静态声明 |
   | `scripts/dev.py` | 依赖扫描只服务 `dev`，不再包含 `dev-CA`；家目录和身份初始化仍覆盖两台 |
   | `dependency-rules.json` | `exclude_paths` 增加 `github_work/cc-runtime/dev-runtimes`，否则扫描会进入 `dev-CA` 的家目录 |

5. 重建：`python3 scripts/dev.py compose up -d --no-deps --force-recreate dev-CA dev-CA-network-init`。

验收，全部通过才算 `dev-CA` 落地：

| 类别 | 检查 | 通过标准 |
| --- | --- | --- |
| 身份 | 重复阶段 2 的身份和网络验收 | 全部不变 |
| 同步 | 在 `dev-CA` 和 Mac 各改一个文件 | 2 秒内到达另一侧 |
| 同步 | 在一个大仓库里切换差异很大的分支，两个方向各一次 | 记录耗时；完成后两侧 `git status` 干净 |
| 同步 | 会话状态 | 没有未解决的冲突和问题 |
| 隔离 | 在 `dev-CA` 里装依赖 | Mac 上对应项目的依赖目录内容不变，Mac 上该项目仍能运行；反方向同样成立 |
| 依赖 | 选 Node、Python、Rust 各一个真实项目，在 `dev-CA` 里从零安装并构建 | 能跑通；记录耗时并和 `dev`（依赖在卷上）对比 |
| 依赖 | `rm -rf node_modules`、`cargo clean` | 成功，不再报挂载点无法删除 |
| git | 在 `dev-CA` 里提交，在 Mac 上提交 | 另一侧立刻看到，状态干净 |
| 并存 | 在 `dev`（旧方案）里改文件 | 经 Mac 的 workspace 同步到 `dev-CA`；`dev` 自身不受影响 |
| 运行期 | 重建 `dev-CA` 容器 | 同步不中断，Linux 依赖还在 |
| 运行期 | 停掉同步守护进程，再启动 | 状态检查能报出「未运行」；启动后自动追平 |
| 运行期 | 重启 Mac（由使用者操作） | 守护进程和会话自动恢复 |
| 观察期 | 用 `dev-CA` 做真实工作 3 天 | 没有原因不明的冲突，每天比对一次两棵树，内容一致 |

依赖耗时这一项没有硬性数字，由使用者看对比结果决定能否接受。不能接受时，按设计文档把同步的 Linux 端换成 Docker 卷，重做本阶段。

回退：先让会话把未同步的改动推完，再把 `dev-CA` 的 workspace 挂载源改回 `/Users/zeke/workspace`、把它加回依赖扫描、重建容器，最后暂停会话。阶段 2 的目录和名字不用退。`linux-workspace` 里的源码此时已经全部在 Mac 的 workspace 里，可以保留也可以删除。

## 阶段 4：`dev` 改为 `dev-PA`，清理旧方案

`dev-CA` 验收全部通过后才开始，届时再把这一节展开成详细步骤。要点：

- 从 Mac 的终端或 `dev-CA` 里执行，不能从 `dev` 容器里发起，重建会终止其中所有会话。
- 对 `dev` 重复阶段 2 和阶段 3：`dev-runtime/` → `dev-runtimes/dev-PA/`，服务名改为 `dev-PA`，workspace 挂到同一个 `linux-workspace`。Tailscale 按 IP `172.30.0.10` 转发，不受改名影响。
- 两台都切换后清理：`scripts/dev.py` 里的扫描和挂载计划、`dependency-rules.json`、`.runtime/` 下的生成文件、对应的测试；Mac 上由 Docker 创建的空挂载点目录（只删空目录）；旧卷里按项目划分的依赖子目录，缓存子目录保留。
- 更新 `README.md` 和 `AGENTS.md`，把设计文档的状态改为已实施。

## 不在本次范围

- `proxy-california` 等代理服务的改名。
- Tailscale 的转发目标。
- 开发镜像本身的内容。
