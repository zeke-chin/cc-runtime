# 依赖隔离：两个方案

状态：方案已定，方案 2 落地中（2026-11-12）。本文取代 README「单卷隔离依赖与构建产物」一节所描述的方案；落地完成前，仓库里运行的仍是旧方案和旧目录布局。进度见 [落地计划](dependency-isolation-rollout.md)。

## 结论

项目保留两个方案，按「Mac 本机要不要一套自己的依赖」来选：

| | 方案 1：纯 Linux | 方案 2：双目录同步 + 外置 git 目录 |
| --- | --- | --- |
| 适用 | 项目只在容器里运行和构建 | 同一个项目在 Mac 和容器里都要运行 |
| 依赖 | 只有 Linux 一套 | Mac 一套，Linux 一套 |
| 源码 | 一份，容器直接挂载 Mac 的 workspace | 两份，Mutagen 双向同步 |
| git 数据 | 一份，在仓库原位 | 一份，移到 workspace 之外，两棵树各用一个链接指向它 |
| 新增的机制 | 无 | 同步会话、git 目录的迁出脚本 |
| 对 Mac 的改动 | 无 | 每个仓库的 `.git` 从目录变成符号链接 |

方案 2 的结构：

```
/Users/zeke/workspace          ⇄  Mutagen 双向同步  ⇄   ~/.cc-runtime/linux-workspace
Mac 的源码 + Mac 的依赖            (在 Mac 本地运行，         Linux 的源码副本 + Linux 的依赖
        │                         依赖目录不参与同步)             │
        │ foo/.git 是链接                                         │ foo/.git 是同一个链接
        └──────────────►  ~/.cc-runtime/git/foo.git  ◄────────────┘
                          唯一的一份 git 数据
```

## 要解决的问题

同一个项目经常既在 Mac 上跑、也在容器（linux-arm64）里跑，两边的依赖和构建产物是平台相关的，不能共用同一个目录。

需求（已和使用者确认）：

1. 源码两边共享，可以接受亚秒级延迟，不要求严格同一个 inode。
2. 两边都长期保有可用的依赖，互不覆盖。
3. 整个 workspace 的任何项目都可能在容器里打开，新建或新 clone 的项目不需要任何登记步骤。
4. 不为此重建容器。
5. 机制尽量轻，出问题时看得见。

## 方案 1：纯 Linux

方案 2 的全部复杂度都来自需求 2。项目不需要在 Mac 本机运行或构建时，不需要方案 2：

- Mac 上的编辑器通过 SSH 远程连进开发容器（Cursor 和 VS Code 的 Remote-SSH、Zed 的远程开发），编辑、语言服务、终端、安装依赖和构建都在容器里进行。
- 容器直接挂载 `/Users/zeke/workspace`，源码和 `.git` 都只有一份，没有同步，也没有屏蔽。
- 依赖只有 Linux 一套，放在项目目录里。Mac 上能看到这些目录，但不使用它们。

适用条件和代价：

- 只适用于不在 Mac 本机运行的项目。只在 Mac 上构建的项目（例如 Xcode 工程）同样没有冲突，照常在 Mac 上用即可。
- 编辑器的语言服务跑在容器里，容器没启动时只能看代码。
- 不支持远程开发的 Mac 工具拿不到可用的依赖。

在本仓库里采用方案 1 的做法：`compose.yaml` 中开发服务的 workspace 挂载源保持 `/Users/zeke/workspace`，不建同步会话，不迁出 git 目录。本部署落地方案 2 之后仍然可以按方案 1 的方式使用：SSH 进容器，只在容器里装依赖。

本文其余部分说明方案 2。

## 旧方案为什么不满足

旧方案是「共享一个目录，再用 Docker 卷的子目录逐个盖住依赖目录」。Docker 的挂载在容器创建时就固定，而屏蔽又要求列出每一个目录，因此下面几点是这个思路自带的，改实现也消不掉：

| 问题 | 2026-11-12 的实际情况 |
| --- | --- |
| 新项目要重新扫描并重建容器才被隔离 | 计划里有 956 个目录，运行中的容器只挂了 499 个，其中 20 个未挂载的目录里已经有内容 |
| 挂载数等于目录数 | 生成的 Compose 附加文件 738 KB，必须通过 `scripts/dev.py` 启动 |
| 必须在目录出现前预判 | Docker 在 Mac 上创建了 767 个空的挂载点目录（空 `.venv` 131、空 `target` 127、空 `bin`/`obj` 各 173 等） |
| 自有机制重 | 扫描器、规则文件、卷内哈希布局和对应测试都要自己维护 |

另外，依赖目录是挂载点，容器里无法删除或重命名它（例如 `rm -rf node_modules`、`cargo clean`）。

## 设计原理

让同一个路径在两个系统上落到不同的存储，只有三种手段：在该路径上挂载、让文件系统按名字路由、放到另一棵树。符号链接不算，它存在共享的树里，两边看到的是同一个。

| 手段 | 用来分开依赖时的结果 |
| --- | --- |
| 在该路径上挂载 | 必须在目录出现前挂好，所以要预判。这就是旧方案 |
| 文件系统按名字路由 | 没有现成的实现，只能自己写，见「没有采用的方案」 |
| 放到另一棵树（本设计） | 其余内容靠同步复制 |

选了两棵树之后有两个决定。

**第一，git 数据怎么办。** 源码可以靠同步复制，`.git` 不能。`.git` 是带锁的数据库，`index` 一个文件里既有暂存内容、又有本侧的文件状态缓存，另一侧只要运行一次 `git status` 就会改写它。实测（见「实测记录」）用文件同步复制 `.git` 会稳定地产生冲突。所以 git 数据只能有一份，两棵树都直接读写它。

这一份放在哪有两种选择：

| | 留在 Mac 仓库原位 | 移到第三处（本设计） |
| --- | --- | --- |
| Mac 上的仓库 | 不变 | `.git` 变成符号链接 |
| Linux 树里的指针 | 脚本逐个补链接；容器里新建的仓库还要把 `.git` 搬到 Mac 树 | 和 Mac 树里的是同一个链接，由同步传播 |
| 容器要挂载的 | 整个 Mac workspace，可写 | 只有 git 目录 |
| 自有脚本的动作 | 两种：补链接、搬迁 | 一种：迁出，两棵树通用 |

两种做法都成立。本设计选第三处：容器不再能写 Mac 的 workspace，链接不需要单独维护，代价是 Mac 上每个仓库的 `.git` 变成链接。

**第二，Linux 那棵树的数据放在哪**，也就是让谁去跨 Mac 和 Linux 虚拟机的边界：

| | 放 Mac 目录（本设计） | 放 Docker 卷 |
| --- | --- | --- |
| 跨边界的是 | 容器的文件读写（virtiofs） | 同步流（经 `docker exec` 和容器内代理） |
| 变更检测 | 两端都在 macOS，原生递归监听 | Linux 端是轮询（默认 10 秒）加少量原生监听 |
| 端点可用性 | 两个本地目录，始终可用 | 依赖容器在运行，要往容器里放同步代理 |
| 文件系统语义 | 两端都是 APFS | 两种文件系统，有大小写和属主转换 |
| 数据可见性 | Mac 上直接可见、可备份 | 在卷里 |
| 容器内依赖读写 | 慢（见「代价与已知限制」） | 原生速度 |

判断依据：同步晚到或悄悄分叉是看不见的大代价，构建慢几秒是看得见的小代价，所以优先保证同步这条路径简单可靠。这也符合 `AGENTS.md` 对持久状态使用宿主机 bind mount 的约定。

## 组成

### 运行时目录规范

属于某台机器的状态放在仓库根下的 `dev-runtimes/`，每台机器一个子目录，取代现在的 `dev-runtime/` 和 `dev-california-runtime/`。所有机器共用的 Linux 副本和 git 目录放在 Mac 的 workspace 之外：

```
<仓库>/dev-runtimes/
    dev-CA/              # 一台开发机，命名为 dev-{代号}
        home/            # 家目录：用户级授权和配置（git、claude 等）
        ssh-host-keys/   # SSH 主机密钥：重建容器后客户端不必重新信任
        machine-id       # 本机的 machine-id
    dev-PA/              # 另一台开发机，结构相同

/Users/zeke/.cc-runtime/
    linux-workspace/     # 同步得到的 workspace 副本，含 Linux 依赖；所有开发机共同挂载
    git/                 # 外置的 git 目录，按仓库在 workspace 内的相对路径存放
        github_work/cc-runtime.git/
```

命名约定：机器目录叫 `dev-{代号}`，代号一般取这台机器的代理落地代号，例如出口在加州的机器叫 `dev-CA`。

| 路径 | 归属 | 容器内挂载点 |
| --- | --- | --- |
| `dev-runtimes/dev-{代号}/home/` | 单台机器 | `/home/dev` |
| `dev-runtimes/dev-{代号}/ssh-host-keys/` | 单台机器 | `/var/lib/cc-runtime/ssh` |
| `dev-runtimes/dev-{代号}/machine-id` | 单台机器 | `/etc/machine-id`、`/var/lib/dbus/machine-id`（只读） |
| `~/.cc-runtime/linux-workspace/` | 所有机器共用 | `/home/dev/workspace` |
| `~/.cc-runtime/git/` | 所有机器和 Mac 共用 | `/Users/zeke/.cc-runtime/git`（和 Mac 上同一路径） |

- `dev-CA` 对应现在的 `dev-california` 服务，`dev-PA` 对应现在的 `dev` 服务。Compose 服务名也改成和机器目录相同的名字。
- `dev-runtimes/` 整体不入 Git，也不进 Docker 构建上下文。
- 各机器目录之间不复制任何内容，新机器的家目录仍只用 `scripts/seed-dev-home.py` 初始化。
- `~/.cc-runtime/linux-workspace/` 从 Spotlight 和 Time Machine 中排除，里面会有大量 Linux 的依赖文件。
- `~/.cc-runtime/git/` 不能从 Time Machine 中排除。迁出之后，所有仓库的提交历史都在这里。

路径的对应关系对使用者不变：Mac 上的 `/Users/zeke/workspace/foo` 仍对应容器里的 `/home/dev/workspace/foo`，只是两者之间隔了一次同步。

`compose.yaml` 里每个开发服务的家目录、SSH 主机密钥和 machine-id 挂载源改为各自的机器目录，workspace 挂载源改为 `/Users/zeke/.cc-runtime/linux-workspace`，并增加 git 目录的同路径挂载。不再需要生成的附加文件，可以直接 `docker compose up`。`~/.cache` 继续放在现有的缓存卷上，改为在 `compose.yaml` 里静态声明；卷里按项目划分的依赖子目录在迁移后废弃。

### 所有开发机共用一份副本

需要分开的是「因系统不同而不同的依赖」。开发容器都是 Linux，并且都跑在同一台 Mac 上、架构相同，它们的依赖可以通用，所以只需要一份 Linux 副本：

- 只有一个同步会话，源码在 Mac 磁盘上存两份，和机器数量无关。
- 依赖在任意一台开发机上装一次，其他开发机直接可用。
- 一台机器里的改动，另一台机器立刻可见，不经过同步。
- 不要在两台开发机上同时对同一个项目安装依赖或构建。

这个前提只在所有开发机系统和架构相同时成立。将来如果出现不同架构的开发机，需要给它单独一份副本和一个会话。

### 副本放在 workspace 之外

副本的位置是 `/Users/zeke/.cc-runtime/linux-workspace`，不在 `/Users/zeke/workspace` 里面。同步的两端互不包含，方案的正确性就不依赖任何一条忽略规则。

没有放在仓库的 `dev-runtimes/` 下，是因为本仓库位于 `/Users/zeke/workspace/github_work/cc-runtime`，那样副本就成了它要同步的那棵树的子目录：一旦忽略规则缺失或写错，Mac 端会把副本再同步进副本，无限嵌套。

位置的选择：

| 候选 | 结论 |
| --- | --- |
| `~/.cache/` | 不用。缓存目录会被清理工具按文件删除，而双向同步会把这些删除当成改动传回 Mac 的源码 |
| `~/.config/` | 不用。那里是小体积的配置，常被 dotfiles 工具和备份整体收走 |
| `~/.cc-runtime/` | 采用。专属目录，归属清楚，和 workspace 在同一个磁盘上 |

机器目录 `dev-runtimes/` 仍在仓库里，也就仍在同步根之内，所以忽略名单里保留 `/github_work/cc-runtime/dev-runtimes/` 这一条。缺了它会多同步一批文件，并让开发机互相看到对方的家目录，但不会造成嵌套。

### 同步会话

- 工具：Mutagen（Mac 上已安装 0.18.1），守护进程运行在 Mac 上，两端都是本地路径。
- 只有一个会话，名为 `linux-workspace`：alpha 端是 `/Users/zeke/workspace`，beta 端是 `/Users/zeke/.cc-runtime/linux-workspace`。
- 模式：`two-way-safe`（Mutagen 的默认值）。不会造成数据丢失的冲突自动解决，其余的记录在会话状态里等人处理，不自动覆盖任何一侧。
- 符号链接：用 `posix-raw` 模式原样传播。默认的 `portable` 模式遇到绝对路径或指向根目录之外的链接会让整个会话停下来，而 `.git` 链接正是绝对路径。
- `.git`：真实目录不同步，符号链接照常同步，见下一节。
- 权限：Mutagen 只传播可执行位，属主是运行它的用户；新建文件和目录的默认权限是 `0600` 和 `0700`，会话里改成 `0644` 和 `0755`。
- 会话定义（两端路径、模式、忽略名单）作为脚本入库，不手工维护。

示意（参数名以 `mutagen sync create --help` 为准）：

```bash
mutagen sync create --name=linux-workspace \
  --symlink-mode=posix-raw \
  --default-file-mode=0644 --default-directory-mode=0755 \
  --ignore-vcs \
  --ignore=/github_work/cc-runtime/dev-runtimes/ \
  --ignore=node_modules --ignore=.venv --ignore=target ... \
  /Users/zeke/workspace \
  /Users/zeke/.cc-runtime/linux-workspace
```

### git 目录外置

每个仓库的 git 数据只有一份，放在 `~/.cc-runtime/git/<仓库在 workspace 内的相对路径>.git`。两棵树里的 `foo/.git` 是同一个绝对路径的符号链接：

```
/Users/zeke/workspace/github_work/foo/.git              ─┐
                                                         ├─► /Users/zeke/.cc-runtime/git/github_work/foo.git
~/.cc-runtime/linux-workspace/github_work/foo/.git      ─┘
```

- 容器把 `~/.cc-runtime/git` 挂到和 Mac 上相同的路径，所以同一个链接在两边都能解析。容器不挂载 Mac 的 workspace。
- 两棵树里的链接内容相同，它由 Mutagen 当作普通符号链接同步，Linux 侧不需要补链接。
- git 的工作区是链接所在的目录，所以 Mac 上的 git 操作 Mac 树里的文件，容器里的 git 操作 Linux 树里的文件，两边读写同一个 `index`、`HEAD` 和对象库，靠 git 自己的锁互斥。

**同步规则只跳过真实的 `.git` 目录。** 用 `--ignore-vcs`，不用 `--ignore=.git`。实测 `--ignore-vcs` 忽略名为 `.git` 的目录，但不忽略同名的符号链接；`--ignore=.git` 两者都忽略。这样：

| 仓库状态 | 同步的行为 |
| --- | --- |
| 已迁出，`.git` 是链接 | 链接同步到另一侧，两边都能用 git |
| 未迁出，`.git` 是真实目录 | 不同步。git 只在这一侧可用，另一侧有源码但不是仓库 |
| 两侧各有一个真实的 `.git` | 互不影响，各自为政。迁出其中一个时另一个会和链接冲突，由人处理 |
| 名为 `.git` 的普通文件（子模块、`worktree` 的 gitfile） | 照常同步，`--ignore-vcs` 只跳过目录 |

未迁出是安全的默认状态：另一侧运行 git 会明确报「不是 git 仓库」，不会损坏数据。

**迁出脚本** `scripts/git-store.py`，在 Mac 上运行。两棵树和 git 目录都在同一块磁盘上，迁出是一次改名加一个链接，不复制数据。

| 命令 | 作用 |
| --- | --- |
| `status` | 列出每个仓库的状态：已迁出、未迁出（在哪一侧）、异常 |
| `externalize <仓库>` / `--all` | 把真实的 `.git` 目录移到 git 目录下，原位换成链接。对两棵树里的仓库都适用，所以容器里新建的仓库也用它 |
| `restore <仓库>` / `--all` | 反向操作，把 git 数据移回 Mac 树里的原位。用于回退 |

`externalize` 的保护条件：git 自己的锁文件存在（`.git` 顶层和 `refs/` 下的 `*.lock`）、正在接收对象（`objects/incoming-*`、`objects/pack/tmp_*`）、`.git` 在最近两分钟内有改动、或者还没有提交时跳过（可能正在 clone 或提交）。只看 git 自己的锁，是因为编辑器会在 `.git` 里放长期存在的文件（实测 Cursor 留有 `.git/cursor/crepe/build.lock`）；目标路径已存在时拒绝；含子模块（有 `.gitmodules` 或 `.git/modules/`）或 `git worktree`（`.git/worktrees/` 非空）的仓库一律跳过，不迁出，原因见「代价与已知限制」。

`status` 报告的异常：

- 链接指向的 git 目录不存在。
- 两个工作区指向同一个 git 目录。用 `cp -R` 复制项目会造成这种情况。
- git 目录下有没有任何工作区指向的孤儿。删除项目时只删掉了链接，git 数据会留下。
- 链接的目标和仓库当前的相对路径不一致。项目改名或移动后链接仍然有效，只是存放位置的名字对不上。
- 已迁出的仓库后来加了子模块或 `worktree`。

**新仓库的迁出。** 需求 3 要求新项目不需要登记，所以由 Mac 上的定时任务（launchd）周期性运行 `externalize --all`。新仓库在被迁出之前只在创建它的那一侧可用 git。定时任务在两台开发机都切换完成后才启用，试点期间手动迁出指定的仓库，原因见落地计划。

**使用约定：**

- 同一个仓库，同一时间只在一侧做 git 写操作（提交、切换分支、暂存、拉取）。只开着编辑器看不算。
- 一侧切换分支或拉取之后，另一侧的 git 数据立刻是新的，文件要等同步到达。这个窗口内不要在另一侧做 git 写操作；不确定时先在 Mac 上运行 `mutagen sync flush linux-workspace`。
- 复制项目用 `git clone`，不要用 `cp -R`。已迁出的仓库不要再 `git worktree add` 或新增子模块；需要时先 `restore`。
- 删除项目后，对应的 git 目录要另外删除，`status` 会列出来。

### 忽略名单

名单声明哪些目录是每一侧私有的。

| 类别 | 条目 | 说明 |
| --- | --- | --- |
| 本部署的运行时状态 | `/github_work/cc-runtime/dev-runtimes/` `/github_work/cc-runtime/tailscale/state/` | 以同步根为锚的路径。家目录、SSH 主机密钥、machine-id 和 Tailscale 状态不进入副本 |
| 平台相关的依赖和构建产物 | `node_modules` `.venv` `target` `.next` `.nuxt` `.output` `.svelte-kit` `.turbo` `.parcel-cache` `.gradle` `.dart_tool` `.angular` `.tox` `.nox` | 任意深度按名字匹配 |
| 没有必要共享的缓存 | `__pycache__` `.pytest_cache` `.ruff_cache` `.mypy_cache` | 各自生成，同步只会增加噪音 |
| 迁移期间的旧机器目录 | `/github_work/cc-runtime/dev-runtime/` `/github_work/cc-runtime/dev-california-runtime/` | `dev` 和 `dev-california` 在迁移完成前仍用它们，里面有家目录、凭据和 SSH 主机私钥。阶段 4 之后目录不存在，条目失效，下次重建会话时删除 |
| 其他容器的运行时状态 | `/github_work/shopify-lens/data/shopify_lens_pg_data/` | 正在运行的 `shopify_lens_pg` 的 Postgres 数据目录（33 GB）。同步活的数据库目录会持续产生负载，并且 Linux 侧的任何改动都会流回它。检查脚本的第 4 条断言会报出新的这类挂载 |
| 体积大的工具缓存 | `/rust_work/.cache/` `/.pnpm-store/` | 阶段 1 发现：前者 11 GB，后者 891 MB，都不是源码 |
| 名字不标准的虚拟环境 | `.venv-mac`（按名字）`/github_work/Confucius4-R2T2/.vnev/` | 检查脚本报出 |

不在名单里的：`bin`、`obj`、`build`、`dist`、`vendor`。这些名字经常就是源码目录，不能按名字全局忽略，默认照常同步；确有需要时为具体项目加带路径的条目。

### 检查脚本

`scripts/check-sync-ignores.py`，在 Mac 侧运行，四个断言：

1. **两端互不包含**：会话的两个端点路径没有包含关系，git 目录不在任何一个端点之内，并且忽略名单包含 `dev-runtimes/` 条目。
2. **没有误伤**：任何被 git 跟踪的文件都不落在忽略名单内。被 git 跟踪的一定是源码，所以这类错误可以确定性地查出来。
3. **没有明显遗漏**：未被忽略的目录里不应出现 `pyvenv.cfg`（即名字不标准的虚拟环境，如现有的 `.venv-mac`、`.vnev`），发现时给出警告。
4. **不同步活的状态**：任何容器以可写方式挂载的、workspace 内的路径都必须被忽略（整个 workspace 的挂载除外）。

2026-11-12 在真实 workspace 上的结果：第 1、3、4 条通过（3、4 条按上面的名单处理后）；第 2 条报出 4 个仓库（含 `old_work` 下的两份副本）把 `__pycache__/*.pyc` 提交进了 git，例如 `wm_work/wos_skills_work`、`wm_work/aisales-doc`。这些文件不会同步，迁出后容器里会显示为已删除；处理办法是在这些仓库里取消跟踪，待使用者确认。

git 目录的检查由 `scripts/git-store.py status` 负责。

### 状态可见性

- Mac 上：`mutagen sync list` 显示会话状态、未解决的冲突和问题；`scripts/git-store.py status` 显示未迁出和异常的仓库。
- 容器里：通过已有的宿主机 SSH 执行同样的命令。
- 守护进程停止时两边会各自继续工作并逐渐分叉，所以启动检查里要包含「会话存在且在运行」。是否在 shell 提示符里显示同步状态，留到试点后决定。

Mutagen 在同步根被整个删除时不把删除传播到另一侧，文档说明这类保护是尽力而为的。实测（0.18.1）的表现是报一个根目录冲突，而不是暂停会话；重建目录后用 `mutagen sync reset` 恢复。

## 实测记录（2026-11-12）

在 Mac 上用临时目录、一个约 2000 个文件的真实仓库和一次性容器测得。

| 项目 | 结果 |
| --- | --- |
| 两个方向的同步延迟 | 各 10 次，中位数 0.05 秒，最长 0.06 秒 |
| 按名字忽略 | 两侧各建的 `node_modules`、`.venv`、`target` 都没有到对面 |
| 容器写入 | 0.05 秒出现在 Mac 侧 |
| Mac 改文件后容器里的文件监听 | 0.12 秒收到通知 |
| 用文件同步复制 `.git` | 不成立。容器暂存、提交、切分支，Mac 穿插 `git status`：12 轮全部出现 `.git/index` 冲突，Mac 显示错误的暂存状态 |
| git 数据只有一份、两棵树共用（当时的指针是 Linux 树链接到 Mac 原位的 `.git`） | 同一流程 12 轮 0 冲突。Mac 后台每 0.2 秒只读 `status`、容器连续 40 轮提交：80 条命令 0 失败。Mac 的 `status` 改为会写索引：80 条里 2 条因 `index.lock` 争用失败并明确报错，下一轮自动补上。`git fsck` 无错误 |
| `--ignore-vcs` 对符号链接 | 不忽略名为 `.git` 的链接。一侧是真实目录、另一侧是链接时报冲突，没有覆盖任何一侧 |
| 两棵树用同一个链接指向第三处的 git 目录（本地 git 2.53，初测） | `git status`、提交正常，`--show-toplevel` 落在各自的树上 |
| 2 万个文件的仓库，换侧后第一次 `git status` | 0.5 到 0.8 秒；同一侧连续执行 37 ms |

| 外置布局完整复核（容器同路径挂载 git 目录，链接经同步传播） | 链接 0.045 秒到达另一侧；当初失败的并发流程 12 轮 0 冲突，压力测试 160 条命令 0 失败；两侧同时提交 20 次 1 次明确的锁报错、0 丢失；容器里新建的仓库迁出后 0.012 秒 Mac 侧可用。详见落地计划阶段 1 |
| 切换相差 998 个文件的分支 | 另一侧 0.53 到 0.71 秒后干净；`flush` 0.63 秒 |
| 1 万个文件的仓库（S2Arm）换侧后第一次 `git status` | Mac 约 0.6 秒，容器 0.7 到 0.84 秒；连续执行约 0.05 秒 |
| 子模块、`worktree` 迁出 | 不成立，见「代价与已知限制」 |

## 仓库里的变化

| 动作 | 对象 |
| --- | --- |
| 迁移 | `dev-california-runtime/` → `dev-runtimes/dev-CA/`，`dev-runtime/` → `dev-runtimes/dev-PA/`；Compose 服务 `dev-california` → `dev-CA`，`dev` → `dev-PA` |
| 删除 | `scripts/dev.py` 中的 `scan`、`make_plan`、`generate`、`initialize` 及相关命令；`dependency-rules.json`；`.runtime/compose.dependencies.json` 和 `.runtime/dependencies.json`；`tests/test_dependencies.py` 中的扫描与卷测试 |
| 保留 | `scripts/dev.py` 中的家目录、SSH 主机密钥、machine-id 初始化 |
| 修改 | `compose.yaml`（各机器的挂载源、git 目录挂载、静态的缓存卷）；`.gitignore` 和 `.dockerignore`（改为排除 `dev-runtimes/`）；`scripts/seed-dev-home.py` 和 `tests/test_identity.py` 中的路径；`README.md` 对应章节；`AGENTS.md` 中关于目录归组、依赖卷和 `scripts/dev.py` 的约定 |
| 新增 | 同步会话的定义、`scripts/check-sync-ignores.py`、`scripts/git-store.py` 及其定时任务 |

## 代价与已知限制

- **Mac 上每个仓库的 `.git` 变成符号链接。** 这是方案 2 对 Mac 使用习惯的唯一改动。`restore` 可以逐个移回。Mac 上各工具对链接形式的 `.git` 是否都正常，还没有验证。
- **新仓库要先迁出，另一侧才能用 git。** 定时任务启用后最多晚一个周期；之前要手动运行。
- **两个新的误用方式。** `cp -R` 复制项目会得到两个共用同一份 git 数据的工作区；删除项目会留下孤儿 git 目录。`status` 能报出来。
- **含子模块或 `git worktree` 的仓库不迁出，在容器里只有源码、不能用 git。** 实测子模块迁出后 `core.worktree` 按 git 目录的真实路径解析而失效，之后的 `submodule deinit/update/add` 又会写回只在一侧有效的相对路径；`worktree` 在容器里被判为可清理，容器里一次 `git gc` 就删掉了 Mac 侧 worktree 的元数据。2026-11-12 真实 workspace 里这类仓库去重后 12 个（含子模块 9 个、含 `worktree` 6 个），需要在容器里用 git 时按方案 1 的方式使用，或者去掉 worktree 后再迁出。
- **已迁出仓库的 `git worktree list` 把主工作区显示为 git 目录的路径。** 其他命令（`rev-parse --show-toplevel` 等）正确。
- **换侧后第一次 `git status` 要重读全部文件。** `index` 只有一份，而两棵树里同一个文件的修改时间不同（Mutagen 不保留修改时间）。两边同时活跃时这是常态。Mac 编辑器的后台刷新如果只读（Cursor 是这样），它每次刷新都要重读，直到 Mac 上运行一次会写索引的 git 命令。
- **git 数据先到、文件后到。** 见「使用约定」。
- **偶发的锁误报。** virtiofs 有缓存，Mac 删除文件后容器最长约 1 秒内仍认为它存在，表现为偶尔报 `index.lock` 已存在，重试即可。旧方案也有同样的现象。
- **容器内依赖读写变慢。** 在本机 OrbStack 上用 2 万个 1 KB 文件做的合成测试：

  | 操作 | Linux 卷 | Mac 目录（virtiofs） |
  | --- | --- | --- |
  | 创建 | 0.3 s | 2.1 s |
  | 遍历并 stat | 0.03 s | 0.2 s |
  | 全部读取 | 0.07 s | 0.7 s |
  | 删除 | 0.13 s | 1.4 s |

  小文件操作慢 6 到 10 倍。预计影响集中在冷装依赖和大项目冷启动，量级是多几秒到十几秒。真实项目上的耗时还没有测过。
- **多一个常驻组件。** Mac 上要运行 Mutagen 守护进程（已安装）。它仍是 1.0 之前的版本。
- **源码在 Mac 磁盘上存两份。** 2026-11-12 实测，排除忽略名单后 workspace 约 26 万个文件、105 GB；去掉 `shopify_lens_pg_data`（33 GB）后约 72 GB 要复制一份。其中 `github_work/shopify-lens/data/oss-history` 一个目录 32 GB（未被 git 跟踪的数据）。git 数据只有一份，合计约 2.5 GB。
- **冲突需要人处理。** 同一文件在同步窗口内被两边同时修改时会被标记，不会自动合并。
- **重命名或移动项目后，另一侧的依赖留在旧路径。** 需要在新路径重新安装，旧路径的残留要手动清理。实测：一侧删除或改名项目时，如果另一侧这个目录里有被忽略的内容，会话报冲突，另一侧整个旧目录（包括源码）都留下，删掉残留后冲突自动消失。
- **规则没有消失。** 仍然要维护一份忽略名单，只是从「启动时按标记文件预判每个目录」变成「十几个名字持续生效，并且有脚本校验」。

## 尚未验证

按重要性排列，都在落地计划的阶段 1 里：

阶段 1 已验证外置布局下的 git、并发、迁出和恢复、子模块和 `worktree`、删除和重命名、根目录保护。剩下的：

1. Mac 上的 Cursor、Zed、Xcode 的界面对链接形式的 `.git` 正常工作（git CLI 已验证，待使用者确认）。
2. 大仓库换侧耗时是否可以接受（已测得，待使用者确认）。
3. 真实项目的依赖安装和构建耗时，与现在放在卷上的对比（阶段 3）。

## 实施步骤

详细步骤、每一步的验收标准和回退方法见 [落地计划](dependency-isolation-rollout.md)。顺序是：

1. 在 Mac 上用临时目录验证同步工具和 git 目录外置。不通过就停下，回到设计。
2. `dev-california` 迁移为 `dev-CA`：目录、服务名。`dev` 这段时间继续用旧方案。
3. `dev-CA` 切换到同步的 workspace，只迁出试点仓库。
4. `dev-CA` 全部验收通过后，`dev` 迁移为 `dev-PA`，迁出全部仓库，启用定时任务，清理旧方案。

`dev` 在第 4 步之前仍直接挂载 Mac 的 workspace，并且没有挂载 git 目录，已迁出的仓库在 `dev` 里无法使用 git。所以第 3 步只迁出不在 `dev` 里使用的试点仓库，本仓库在第 4 步才迁出。

回退：`scripts/git-store.py restore --all` 把 git 数据移回原位，旧卷保留到第 4 步，所以任何一步都可以退回旧方案。

如果依赖读写速度不可接受，可以只把同步的 beta 端换成 Docker 卷，忽略名单和检查脚本不变，代价见「设计原理」中的对比。

## 已定事项（2026-11-12）

- 保留两个方案；本部署落地方案 2。
- git 数据外置到 `~/.cc-runtime/git/`，两棵树用同一个链接指向它。使用者此前否决过这个做法（理由是不改动 Mac 上的仓库），在比较了「留在 Mac 原位」需要的补链接脚本和可写挂载之后改为采用。
- 所有顶层目录都参与同步。实测 `models` 3.8 GB，其余几个不到 20 MB；`rust_work` 里只有一个 11 GB 的 `.cache`，加进忽略名单。阶段 3 首次同步时发现，按目录量体积漏掉了 `github_work` 里的大数据目录，见「代价与已知限制」；是否为 `shopify-lens/data/oss-history` 这类数据目录加忽略条目，待使用者确认。
- `dev-CA` 的 `hostname` 保持 `dev-california`，它属于这台机器对外的身份。
- 本仓库的 `.env`、`proxy/config.yaml`、`proxy/config-california.yaml` 不从副本中排除，容器里仍可按 `AGENTS.md` 的流程同时修改派生配置。代价是容器仍能读到代理配置和凭据，和现状相同。
- `machine-id` 放在机器目录下，和 `home/` 同级。

## 待定事项

- 机器目录 `dev-runtimes/` 是否也移到 `~/.cc-runtime/` 下。移过去后同步根里不再有任何运行时状态，可以去掉对应的忽略条目；代价是这些状态不再和仓库放在一起。
- .NET 的 `bin`/`obj` 是否需要按项目加入忽略名单。Mac 侧目前没有这类项目的构建产物。
- 定时迁出的周期，以及是否改为由文件事件触发。
- 是否换用能保留修改时间的同步工具来消除换侧重读。Unison 的 `times` 选项加上 `core.checkStat=minimal` 按原理可以做到，但它没有官方的 macOS 文件监听，也没有常驻进程和状态命令。先在最大的仓库上量换侧耗时，不能接受再评估。

## 没有采用的方案

| 方案 | 不采用的原因 |
| --- | --- |
| `.git` 随源码一起同步 | 实测不成立，见「实测记录」。换成保留修改时间的工具只能缩小窗口，锁和「一次提交改多个文件」的边界仍然传不过去 |
| git 数据留在 Mac 原位，Linux 树链接过去 | 成立，实测通过。没有采用是因为容器必须可写地挂载整个 Mac workspace，并且 Linux 侧的链接和容器里新建仓库的搬迁都要靠脚本。不想改动 Mac 仓库时可以退到这个做法 |
| 只同步 `.git` 的一部分，`index` 每侧一份 | 一侧提交后，另一侧的暂存区过期，`git status` 显示一份反向的改动 |
| 用 `git worktree` 或 `commondir` 让 git 自己生成指针 | `HEAD` 和暂存区变成每侧一份，两侧状态对不上 |
| 自写按名字路由的文件系统（FUSE），只用一棵树 | 模型上最简单，但所有源码读写都要经过自写代码，容器要加 `SYS_ADMIN`，并且按原理 Mac 侧改动的变更通知传不进容器（现在 0.12 秒能收到），容器里的热更新和 watch 会失效 |
| overlayfs，Mac 树做下层、Linux 卷做上层 | 依赖能自动落到上层，但源码和 `.git` 的写入也全落在上层，到不了 Mac |
| 副本放 Docker 卷，同步进容器 | 见「设计原理」。保留为依赖速度不可接受时的后备 |
| 副本放在仓库的 `dev-runtimes/` 下 | 副本会位于同步根之内，正确性依赖一条忽略规则 |
| 每台开发机各一份副本 | 开发机都是同一系统和架构，依赖没有差别；分开只会让源码份数、同步会话数和依赖安装次数都随机器数增加 |
| 工具级重定向（`UV_PROJECT_ENVIRONMENT`、`CARGO_TARGET_DIR` 等） | Node 没有等价机制，`node_modules` 必须同名并位于项目旁，无法覆盖全部 |
| 在容器内动态挂载 | 挂载必须先于目录的创建，仍然要预判；还需要给开发容器 `SYS_ADMIN` 或用特权 sidecar |
| 各自 checkout，靠 git 互通 | 未提交的改动互不可见，不符合「自然共享」的需求 |
| 容器里的 git 转发到 Mac 执行 | 钩子、git 身份、直接读 `.git` 的工具都会错位；容器刚写的文件还没同步到 Mac 时提交会漏掉改动 |

## 参考

- [Mutagen 同步概览](https://mutagen.io/documentation/synchronization/)
- [Mutagen 文件监听](https://mutagen.io/documentation/synchronization/watching)
- [Mutagen 忽略规则](https://mutagen.io/documentation/synchronization/ignores)
- [Mutagen 安全机制](https://mutagen.io/documentation/synchronization/safety-mechanisms/)
- [Mutagen 对版本控制目录的建议](https://mutagen.io/documentation/synchronization/version-control-systems/)
- [Mutagen 符号链接](https://mutagen.io/documentation/synchronization/symbolic-links)、[权限](https://mutagen.io/documentation/synchronization/permissions)
