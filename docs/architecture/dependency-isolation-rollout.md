# 依赖隔离落地计划

状态：执行中。阶段 0 已完成；阶段 1 做了一部分后更换了 git 数据的做法，剩余检查待做（2026-11-12）。设计见 [依赖隔离：两个方案](dependency-isolation.md)，本计划落地其中的方案 2。

## 总体安排

| 阶段 | 内容 | 影响范围 |
| --- | --- | --- |
| 0 | 准备：提交现有改动、定下几个决定 | 只动仓库，不动容器。已完成 |
| 1 | 用临时目录验证同步工具和 git 目录外置 | 只动 Mac 上的临时目录，不动容器，不碰真实 workspace |
| 2 | `dev-california` 改为 `dev-CA`：目录和服务名 | 只重建这一台，workspace 仍按旧方案 |
| 3 | `dev-CA` 切换到同步的 workspace，迁出试点仓库，并验收 | 只重建 `dev-CA`；只改动试点仓库的 `.git`；`dev` 继续用旧方案 |
| 4 | `dev` 改为 `dev-PA` 并切换，迁出全部仓库，然后清理旧方案 | `dev-CA` 验收全部通过后才开始 |

规则：

- 每个阶段都有验收标准，不通过就停在当前阶段，不进入下一个。
- 阶段 1 是整个方案的闸门。不通过时不是修修补补往下走，而是回到设计。
- 每个阶段开始前要得到使用者的明确同意。
- `dev` 在阶段 4 之前不重建。迁移期间所有 Compose 命令都要写明服务名；不带服务名的 `up` 会重建 `dev`，而不经过 `scripts/dev.py` 的 `up` 还会丢掉它的依赖挂载。
- **阶段 4 之前，不迁出本仓库和任何正在 `dev` 里使用的仓库。** `dev` 直接挂载 Mac 的 workspace，又没有挂载 git 目录，迁出后的 `.git` 链接在 `dev` 里是断的。执行落地的会话就运行在 `dev` 里的本仓库中，迁出本仓库会让它自己无法使用 git。
- 真实仓库的 `.git` 只通过 `scripts/git-store.py` 改动，不手工移动。

## 当前状态（2026-11-12）

| 项目 | 状态 |
| --- | --- |
| 容器 | `dev`、`dev-california`、两个 proxy、tailscale 都在运行 |
| Docker / Compose | 29.4.0 / 5.1.2。已验证 Compose 接受 `dev-CA` 这样含大写字母的服务名 |
| Mutagen | Mac 上已安装 0.18.1，守护进程在运行。只有一个临时会话 `spike` |
| Mac 编辑器 | Cursor、Zed、Xcode。Cursor 的后台 `git status` 只读（`GIT_OPTIONAL_LOCKS=0`） |
| `/Users/zeke/.cc-runtime` | 只有阶段 1 的临时内容，见下一行。`linux-workspace/` 和 `git/` 还不存在 |
| 阶段 1 的临时环境 | `~/.cc-runtime/spike/` 下有 `mac/` 和 `linux/` 两棵树，里面是 `fetch_tencent_docs` 的副本和一个测试用的 `newrepo`；会话 `spike`；一次性容器 `spike-c`；`~/.cc-runtime/` 下的若干测试脚本（`gitops.sh`、`race3s.sh`、`stress.sh` 等）。它现在的配置是上一种做法（`--ignore=.git`，Linux 树的 `.git` 链接到 `mac/` 里的真实目录），做阶段 1 剩余检查前要改成外置的布局 |
| 仓库 | 文档已更新为两个方案并提交，工作区干净 |
| 执行位置 | Docker、Homebrew、同步工具的命令都在 Mac 上执行，从 `dev` 容器经 `ssh -p 10023 zeke@172.30.0.2` 发起，Mac 上需要先 `export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH`。执行落地的会话运行在 `dev` 里，所以阶段 4 的重建不能从这里发起 |
| 容器与镜像 | 容器名 `cc-runtime-dev-1`、`cc-runtime-dev-california-1`；开发镜像 `ghcr.io/zeke-chin/cc-runtime:latest`，用户 `dev` 的 uid/gid 为 1001 |
| 一次性测试容器 | 不经过 Compose，直接 `docker run -d --rm --network none --entrypoint sleep -u 1001:1001 -v ... <镜像> infinity`，测完 `docker rm -f` |
| 实测中踩过的坑 | Mac 的 `/tmp` 是指向 `/private/tmp` 的链接，挂载时用 `pwd -P` 得到的真实路径；开发镜像里没有 `bc`，计时用 `date +%s%N`；virtiofs 有缓存，Mac 删除文件后容器里最长约 1 秒仍能看到它，紧接着创建同名文件可能失败；直接给 `ssh` 传命令时远端是 zsh，未加引号的变量不按空格拆分，多行脚本用 `ssh ... 'bash -s' <<'EOF'`；一次性容器里的 git 要加 `-c safe.directory=*` |

## 阶段 0：准备（已完成）

1. 提交仓库里现有的改动。之后每个阶段的修改各自提交，回退时用 Git 就能恢复。
2. 确认的决定：
   - `machine-id` 放在机器目录下，和 `home/` 同级。
   - `dev-CA` 的 `hostname` 不从 `dev-california` 改掉。
   - 本仓库的 `.env` 和代理配置不从 Linux 副本中排除。
   - 所有顶层目录都参与同步。

## 阶段 1：验证同步工具和 git 目录外置

目的：在不碰容器和真实 workspace 的前提下，确认「两棵树 + 同步 + 外置的 git 目录」对源码和 git 都成立。

### 已有的结果

| 检查 | 结果 |
| --- | --- |
| 两个方向的基本同步和延迟 | 通过。各 10 次，中位数 0.05 秒，最长 0.06 秒 |
| 按名字忽略 | 通过。两侧各建的 `node_modules`、`.venv`、`target` 都没有到对面 |
| 容器写入能被感知 | 通过。0.05 秒出现在 `mac/` |
| `.git` 随源码同步 | 不通过，这个做法已放弃。容器暂存、提交、切分支，Mac 穿插 `git status`：12 轮全部出现 `.git/index` 冲突 |
| git 数据只有一份、两棵树共用（指针是 Linux 树链接到 Mac 原位的 `.git`） | 同一流程 12 轮 0 冲突；压力测试 80 条命令里最多 2 条因 `index.lock` 争用失败并明确报错；`git fsck` 无错误 |
| `--ignore-vcs` 对符号链接 | 不忽略名为 `.git` 的链接；一侧真实目录、一侧链接时报冲突，不覆盖任何一侧 |

### 剩余的检查

准备：终止会话 `spike`，删除容器 `spike-c`，把临时环境改成外置的布局后重建。

- 新建 `~/.cc-runtime/spike/git/`。把 `mac/fetch_tencent_docs/.git` 这个真实目录移到 `spike/git/fetch_tencent_docs.git`，原位建一个指向它的绝对路径链接；删掉 `linux/fetch_tencent_docs/.git` 这个旧链接，让新链接由同步带过去。`newrepo` 可以整个删掉重做。
- 会话参数按设计文档：`--symlink-mode=posix-raw`、`0644/0755`、`--ignore-vcs`，加上忽略名单。
- 容器把 `linux/` 挂到 `/w`，把 `spike/git/` 挂到和 Mac 上相同的绝对路径。不再挂载 `mac/`。
- 迁出和恢复先用手工命令验证；`scripts/git-store.py` 在阶段 3 写，写完后用临时环境再过一遍 1.2、1.4、1.5、1.10。

| 编号 | 检查 | 通过标准 |
| --- | --- | --- |
| 1.1 | 忽略规则 | 真实的 `.git` 目录不出现在另一侧；`.git` 链接 2 秒内出现在另一侧，内容相同。另记录名为 `.git` 的普通文件（子模块和 `worktree` 用的那种）是否被同步 |
| 1.2 | 迁出 | 在 `mac/` 迁出一个仓库后，两侧 `git status` 干净、提交一致，`git rev-parse --show-toplevel` 各自落在自己的树上，会话没有冲突 |
| 1.3 | 并发流程 | 按新布局改写并重跑 `race3s.sh`（12 轮）和 `stress.sh`（两种模式各 40 轮）。没有同步冲突，结束后两侧一致，`git fsck` 无错误；命令失败只允许是明确报错的锁争用 |
| 1.4 | 容器里新建仓库 | 容器在 `/w` 下 `git init` 并提交。迁出前 Mac 侧有源码、不是仓库；在 `linux/` 里迁出后，Mac 侧 2 秒内可用 git，提交一致 |
| 1.5 | 恢复 | 把一个已迁出的仓库恢复成真实目录：Mac 侧 git 正常，另一侧的链接随同步消失，会话没有冲突。记录恢复步骤 |
| 1.6 | 两侧同时写 | 两侧在同一秒内各提交一次。至多一侧因锁失败并明确报错，没有提交丢失，`git fsck` 无错误 |
| 1.7 | git 数据先到、文件后到 | 一侧切换到差异较大的分支，记录另一侧从「显示有改动」到干净的时间；`mutagen sync flush` 之后立刻干净 |
| 1.8 | 换侧耗时 | 把 workspace 里文件最多的仓库（`github_work/switch-games/S2Arm`，约 1 万个文件）复制进 `mac/` 并迁出，两侧交替执行 `git status`，记录换侧后第一次和连续执行的耗时，由使用者判断能否接受 |
| 1.9 | Mac 编辑器 | Mac 上用 Cursor 打开 `mac/` 里已迁出的仓库（保留自动 `fetch`），确认源代码管理面板、提交、分支切换正常；同时容器连续修改、提交、切换分支，没有冲突，两侧一致。Zed 和 Xcode 各确认一次能识别仓库。需要使用者参与 |
| 1.10 | 误用的表现 | `cp -R` 复制一个已迁出的项目、删除一个已迁出的项目、给一个已迁出的项目改名，记录各自的结果，确认都能从文件系统上判断出来（供 `status` 实现） |
| 1.11 | 子模块和 `worktree` | 各造一个带子模块的仓库和一个带 `git worktree` 的仓库，迁出后在两侧运行 `git status`、`git submodule status`、`git worktree list`、`git gc`。记录哪些正常、哪些不正常，得出「可以迁出」「迁出时需要额外处理」或「不迁出」的结论。再统计真实 workspace 里这两类仓库各有多少 |
| 1.12 | 符号链接 | 相对链接、绝对链接、指向根目录之外的链接都原样到达，会话不停 |
| 1.13 | 删除和重命名 | 一侧删除或重命名一个含有被忽略目录的项目，记录另一侧留下什么 |
| 1.14 | 根目录保护 | 删除 `linux/` 整个目录，会话暂停，`mac/` 的内容没有被删除 |

阶段结论三选一：

- **通过**：记下最终的参数、迁出和恢复的步骤、使用约定，更新设计文档，进入阶段 2。
- **换做法后通过**：只有 git 数据的做法需要换时适用。退到设计文档「没有采用的方案」里的「git 数据留在 Mac 原位」，并相应改写设计文档和本计划的阶段 3、4。换之前要得到使用者同意。
- **不通过**：停止，回到设计，不进入阶段 2。

结束时无论结果如何都删除临时会话、容器、`~/.cc-runtime/spike/` 和 `~/.cc-runtime/` 下的测试脚本。

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

1. 创建 `/Users/zeke/.cc-runtime/linux-workspace` 和 `/Users/zeke/.cc-runtime/git`。前者从 Spotlight 和 Time Machine 中排除，后者不排除。
2. 写入库的脚本：创建和查看同步会话的脚本、`scripts/check-sync-ignores.py`、`scripts/git-store.py`（`status`、`externalize`、`restore`，保护条件见设计文档）及其测试。用阶段 1 的方法在临时目录里把 `git-store.py` 过一遍。运行检查脚本，处理它报告的误伤和遗漏。
3. 和使用者一起选定试点仓库：3 到 5 个，覆盖 Node、Python、Rust，都不在 `dev` 里使用，不含本仓库。
4. 创建会话，等首次同步完成。完成的标准是会话状态里没有冲突和问题，并且两棵树在排除忽略名单后内容一致。此时 Linux 树里没有任何 `.git`。
5. 用 `scripts/git-store.py externalize` 迁出试点仓库，确认链接同步到了 Linux 树。
6. 修改配置：

   | 文件 | 改动 |
   | --- | --- |
   | `compose.yaml` | `dev-CA` 的 workspace 挂载源改为 `/Users/zeke/.cc-runtime/linux-workspace`；增加 `/Users/zeke/.cc-runtime/git` 到同一路径的挂载；`~/.cache` 的卷挂载和两个缓存环境变量从生成文件移到这里静态声明 |
   | `scripts/dev.py` | 依赖扫描只服务 `dev`，不再包含 `dev-CA`；家目录和身份初始化仍覆盖两台 |
   | `dependency-rules.json` | `exclude_paths` 增加 `github_work/cc-runtime/dev-runtimes`，否则扫描会进入 `dev-CA` 的家目录 |

7. 重建：`python3 scripts/dev.py compose up -d --no-deps --force-recreate dev-CA dev-CA-network-init`。

这个阶段不启用定时迁出。未迁出的仓库在 `dev-CA` 里有源码但不能用 git，这是试点期间的预期状态。

验收，全部通过才算 `dev-CA` 落地：

| 类别 | 检查 | 通过标准 |
| --- | --- | --- |
| 身份 | 重复阶段 2 的身份和网络验收 | 全部不变 |
| 同步 | 在 `dev-CA` 和 Mac 各改一个文件 | 2 秒内到达另一侧 |
| 同步 | 在一个试点仓库里切换差异很大的分支，两个方向各一次 | 记录耗时；完成后两侧 `git status` 干净 |
| 同步 | 会话状态 | 没有未解决的冲突和问题 |
| 隔离 | 在 `dev-CA` 里装依赖 | Mac 上对应项目的依赖目录内容不变，Mac 上该项目仍能运行；反方向同样成立 |
| 依赖 | 试点里的 Node、Python、Rust 项目，在 `dev-CA` 里从零安装并构建 | 能跑通；记录耗时并和 `dev`（依赖在卷上）对比 |
| 依赖 | `rm -rf node_modules`、`cargo clean` | 成功，不再报挂载点无法删除 |
| git | 试点仓库在 `dev-CA` 里提交、推送，在 Mac 上提交 | 另一侧立刻看到，状态干净；`claude` 在 `dev-CA` 的试点仓库里能识别仓库和分支 |
| git | 在 `dev-CA` 里 clone 一个新仓库，再在 Mac 上迁出它 | 迁出后 Mac 侧可用 git |
| git | `scripts/git-store.py status` | 试点仓库显示已迁出，没有异常 |
| 并存 | 在 `dev`（旧方案）里改一个未迁出仓库的文件 | 经 Mac 的 workspace 同步到 `dev-CA`；`dev` 自身不受影响，`dev` 里未迁出的仓库 git 正常 |
| 运行期 | 重建 `dev-CA` 容器 | 同步不中断，Linux 依赖还在，git 仍可用 |
| 运行期 | 停掉同步守护进程，再启动 | 状态检查能报出「未运行」；启动后自动追平 |
| 运行期 | 重启 Mac（由使用者操作） | 守护进程和会话自动恢复 |
| 观察期 | 用 `dev-CA` 在试点仓库上做真实工作 3 天 | 没有原因不明的冲突，每天比对一次两棵树，内容一致 |

依赖耗时这一项没有硬性数字，由使用者看对比结果决定能否接受。不能接受时，按设计文档把同步的 Linux 端换成 Docker 卷，重做本阶段。

回退：先让会话把未同步的改动推完，用 `scripts/git-store.py restore` 恢复试点仓库，再把 `dev-CA` 的 workspace 挂载源改回 `/Users/zeke/workspace`、去掉 git 目录挂载、把它加回依赖扫描、重建容器，最后暂停会话。阶段 2 的目录和名字不用退。

## 阶段 4：`dev` 改为 `dev-PA`，迁出全部仓库，清理旧方案

`dev-CA` 验收全部通过后才开始，届时再把这一节展开成详细步骤。要点：

- 从 Mac 的终端或 `dev-CA` 里执行，不能从 `dev` 容器里发起，重建会终止其中所有会话。
- 顺序：先停掉 `dev`，再迁出全部仓库（含本仓库），然后以 `dev-PA` 启动。这样没有哪个运行中的容器会看到断掉的链接。
- 对 `dev` 重复阶段 2 和阶段 3：`dev-runtime/` → `dev-runtimes/dev-PA/`，服务名改为 `dev-PA`，workspace 挂到同一个 `linux-workspace`，并挂载 git 目录。Tailscale 按 IP `172.30.0.10` 转发，不受改名影响。
- 按阶段 1 的结论处理含子模块和 `worktree` 的仓库。
- 启用定时迁出（launchd），验证新 clone 的仓库在一个周期内两侧都可用，并且不会迁出正在 clone 的仓库。
- 两台都切换后清理：`scripts/dev.py` 里的扫描和挂载计划、`dependency-rules.json`、`.runtime/` 下的生成文件、对应的测试；Mac 上由 Docker 创建的空挂载点目录（只删空目录）；旧卷里按项目划分的依赖子目录，缓存子目录保留。
- 更新 `README.md` 和 `AGENTS.md`，把设计文档的状态改为已实施。

## 不在本次范围

- `proxy-california` 等代理服务的改名。
- Tailscale 的转发目标。
- 开发镜像本身的内容。
