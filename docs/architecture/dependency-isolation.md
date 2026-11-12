# 依赖隔离：双目录同步

状态：设计稿，尚未实施（2026-11-12）。本文取代 README「单卷隔离依赖与构建产物」一节所描述的方案；实施完成前，仓库里运行的仍是旧方案和旧目录布局。

## 结论

依赖的差异来自操作系统，不来自机器。所以按系统各放一棵目录树，Mac 一棵、Linux 一棵，只同步源码：

```
/Users/zeke/workspace          ⇄  Mutagen 双向同步  ⇄   ~/.cc-runtime/linux-workspace
Mac 的源码 + Mac 的依赖            (在 Mac 本地运行，         Linux 的源码副本 + Linux 的依赖
                                  依赖目录不参与同步)             │ bind mount
                                                                  ▼
                                                  每个开发容器内 /home/dev/workspace
```

- Mac 侧的工具继续在 `/Users/zeke/workspace` 里工作，一切不变。
- 所有开发容器都是 Linux，共同挂载同一个 `~/.cc-runtime/linux-workspace` 到各自的 `/home/dev/workspace`。
- 两棵树由 Mac 上的一个 Mutagen 会话双向同步，`node_modules`、`.venv`、`target` 等目录按名字忽略，于是 Mac 和 Linux 各留一份自己的依赖。

## 要解决的问题

同一个项目经常既在 Mac 上跑、也在容器（linux-arm64）里跑，两边的依赖和构建产物是平台相关的，不能共用同一个目录。

需求（已和使用者确认）：

1. 源码两边共享，可以接受亚秒级延迟，不要求严格同一个 inode。
2. 两边都长期保有可用的依赖，互不覆盖。
3. 整个 workspace 的任何项目都可能在容器里打开，新建或新 clone 的项目不需要任何登记步骤。
4. 不为此重建容器。
5. 机制尽量轻，出问题时看得见。

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

同一路径放不了两份内容，所以只有两条路：在一棵树里按路径屏蔽（旧方案），或者两棵树加复制。本设计选后者。

两棵树的方案里，唯一的选择是 Linux 那棵树的数据放在哪，也就是让谁去跨 Mac 和 Linux 虚拟机的边界：

| | 放 Mac 目录（本设计） | 放 Docker 卷 |
| --- | --- | --- |
| 跨边界的是 | 容器的文件读写（virtiofs） | 同步流（经 `docker exec` 和容器内代理） |
| 变更检测 | 两端都在 macOS，原生递归监听 | Linux 端是轮询（默认 10 秒）加少量原生监听 |
| 端点可用性 | 两个本地目录，始终可用 | 依赖容器在运行，要往容器里放同步代理 |
| 文件系统语义 | 两端都是 APFS | 两种文件系统，有大小写和属主转换 |
| 数据可见性 | Mac 上直接可见、可备份 | 在卷里 |
| 容器内依赖读写 | 慢（见「代价与已知限制」） | 原生速度 |

判断依据：同步晚到或悄悄分叉是看不见的大代价，构建慢几秒是看得见的小代价，所以优先保证同步这条路径简单可靠。放 Mac 目录在同步可靠性的三项上都占优，代价只有依赖读写速度。

这也符合 `AGENTS.md` 对持久状态使用宿主机 bind mount 的约定。

## 组成

### 运行时目录规范

属于某台机器的状态放在仓库根下的 `dev-runtimes/`，每台机器一个子目录，取代现在的 `dev-runtime/` 和 `dev-california-runtime/`。所有机器共用的 Linux workspace 副本放在 Mac 的 workspace 之外：

```
<仓库>/dev-runtimes/
    dev-CA/              # 一台开发机，命名为 dev-{代号}
        home/            # 家目录：用户级授权和配置（git、claude 等）
        ssh-host-keys/   # SSH 主机密钥：重建容器后客户端不必重新信任
        machine-id       # 本机的 machine-id
    dev-PA/              # 另一台开发机，结构相同

/Users/zeke/.cc-runtime/
    linux-workspace/     # 同步得到的 workspace 副本，含 Linux 依赖；所有开发机共同挂载
```

命名约定：机器目录叫 `dev-{代号}`，代号一般取这台机器的代理落地代号，例如出口在加州的机器叫 `dev-CA`。这样从目录名就能看出它对应哪条出口。

| 路径 | 归属 | 容器内挂载点 |
| --- | --- | --- |
| `dev-runtimes/dev-{代号}/home/` | 单台机器 | `/home/dev` |
| `dev-runtimes/dev-{代号}/ssh-host-keys/` | 单台机器 | `/var/lib/cc-runtime/ssh` |
| `dev-runtimes/dev-{代号}/machine-id` | 单台机器 | `/etc/machine-id`、`/var/lib/dbus/machine-id`（只读） |
| `~/.cc-runtime/linux-workspace/` | 所有机器共用 | `/home/dev/workspace` |

- `dev-CA` 对应现在的 `dev-california` 服务，`dev-PA` 对应现在的 `dev` 服务。Compose 服务名也改成和机器目录相同的名字。
- `machine-id` 不在最初给出的目录清单里，这里按现有约定保留在机器目录下。
- `dev-runtimes/` 整体不入 Git，也不进 Docker 构建上下文。
- 各机器目录之间不复制任何内容，新机器的家目录仍只用 `scripts/seed-dev-home.py` 初始化。
- `~/.cc-runtime/linux-workspace/` 需要从 Spotlight 和 Time Machine 中排除，里面会有大量 Linux 的依赖文件。

路径的对应关系对使用者不变：Mac 上的 `/Users/zeke/workspace/foo` 仍对应容器里的 `/home/dev/workspace/foo`，只是两者之间隔了一次同步。

`compose.yaml` 里每个开发服务的家目录、SSH 主机密钥和 machine-id 挂载源改为各自的机器目录，workspace 挂载源都改为 `/Users/zeke/.cc-runtime/linux-workspace`。不再需要生成的附加文件，可以直接 `docker compose up`。`~/.cache` 继续放在现有的缓存卷上，改为在 `compose.yaml` 里静态声明；卷里按项目划分的依赖子目录在迁移后废弃。

### 所有开发机共用一份副本

需要分开的是「因系统不同而不同的依赖」。开发容器都是 Linux，并且都跑在同一台 Mac 上、架构相同，它们的依赖可以通用，所以只需要一份 Linux 副本：

- 只有一个同步会话，源码在 Mac 磁盘上存两份，和机器数量无关。
- 依赖在任意一台开发机上装一次，其他开发机直接可用。这和旧方案里两台机器共用一套依赖卷是一致的。
- 一台机器里的改动，另一台机器立刻可见，不经过同步。
- 不要在两台开发机上同时对同一个项目安装依赖或构建。

这个前提只在所有开发机系统和架构相同时成立。将来如果出现不同架构的开发机，需要给它单独一份副本和一个会话。

### 副本放在 workspace 之外

副本的位置是 `/Users/zeke/.cc-runtime/linux-workspace`，不在 `/Users/zeke/workspace` 里面。同步的两端互不包含，方案的正确性就不依赖任何一条忽略规则。

没有放在仓库的 `dev-runtimes/` 下，是因为本仓库位于 `/Users/zeke/workspace/github_work/cc-runtime`，那样副本就成了它要同步的那棵树的子目录：一旦忽略规则缺失或写错，Mac 端会把副本再同步进副本，无限嵌套；Mac 端的监听还会收到副本里每一次依赖安装产生的事件。

位置的选择：

| 候选 | 结论 |
| --- | --- |
| `~/.cache/` | 不用。缓存目录会被清理工具按文件删除，而双向同步会把这些删除当成改动传回 Mac 的源码 |
| `~/.config/` | 不用。那里是小体积的配置，常被 dotfiles 工具和备份整体收走，不适合放上百 GB 的代码和依赖 |
| `~/.cc-runtime/` | 采用。专属目录，归属清楚，和 workspace 在同一个磁盘上 |

机器目录 `dev-runtimes/` 仍在仓库里，也就仍在同步根之内，所以忽略名单里保留 `/github_work/cc-runtime/dev-runtimes/` 这一条。它现在只是防止各机器的家目录和密钥被复制进副本：缺了它会多同步一批文件，并让开发机互相看到对方的家目录，但不会造成嵌套。

### 同步会话

- 工具：Mutagen（Mac 上已安装 0.18.1），守护进程运行在 Mac 上，两端都是本地路径。`.git` 随源码同步已做初步实测，见下面的「`.git` 的同步」，最终以落地计划阶段 1 的结论为准。
- 只有一个会话，名为 `linux-workspace`：alpha 端是 `/Users/zeke/workspace`，beta 端是 `/Users/zeke/.cc-runtime/linux-workspace`。
- 模式：`two-way-safe`（Mutagen 的默认值）。不会造成数据丢失的冲突自动解决，其余的记录在会话状态里等人处理，不自动覆盖任何一侧。
- 符号链接：用 `posix-raw` 模式原样传播。默认的 `portable` 模式遇到绝对路径或指向根目录之外的链接会让整个会话停下来。
- 权限：Mutagen 只传播可执行位，属主是运行它的用户；新建文件和目录的默认权限是 `0600` 和 `0700`，会话里改成 `0644` 和 `0755`。
- 会话定义（两端路径、模式、忽略名单）作为脚本入库，不手工维护。

示意（参数名以 `mutagen sync create --help` 为准）：

```bash
mutagen sync create --name=linux-workspace \
  --symlink-mode=posix-raw \
  --default-file-mode=0644 --default-directory-mode=0755 \
  --ignore=/github_work/cc-runtime/dev-runtimes/ \
  --ignore=node_modules --ignore=.venv --ignore=target ... \
  /Users/zeke/workspace \
  /Users/zeke/.cc-runtime/linux-workspace
```

### `.git` 的同步

两边都要用 git（Mac 上的编辑器，容器里 Claude Code 的提交和推送），所以两棵树都得有可用的 `.git`，并且看到同一个提交历史、分支和暂存状态。做法是让 `.git` 和源码一起同步，不需要额外的链接、挂载或 git 设置。

Mutagen 的官方文档不建议同步 `.git`，DDEV 等基于 Mutagen 的工具默认也不同步。下面说明在本设计的用法下，为什么仍然成立，以及要守的约定。

**索引会被来回改写，但结果不变。** `.git/index` 里每个文件有两类信息：一是暂存内容的哈希，决定 git 的结果；二是文件状态缓存（修改时间、大小、inode 等），只用来加速。Mutagen 不保留修改时间（已实测），所以索引同步到另一侧后，状态缓存全都对不上。另一侧的 git 会重读文件，确认内容没变，再把缓存改成本侧的值写回索引，这份索引又被同步回来。改写前后暂存内容不变，所以结果正确。代价是每次换到另一侧使用后，第一次 git 命令要重读全部文件。

**`git status` 为什么会写索引。** `status` 本身是只读查询，写回刷新后的状态缓存只是顺手做的优化，git 称之为 optional lock。设置 `GIT_OPTIONAL_LOCKS=0` 或加 `--no-optional-locks` 时，`status` 只读不写。Cursor 的 git 扩展在后台运行 `status` 时就设置了它，所以 Mac 上开着 Cursor 时，Mac 这一侧基本不写 `.git`。

**真正的风险是两侧同时写。** 两侧在同一个同步周期内都改写了 `.git` 里的同一个文件时，`two-way-safe` 会把它标为冲突，停止同步这个文件，不会用旧的覆盖新的。冲突在 `mutagen sync list` 里可见，需要人处理。另外，一次提交会改动多个文件，它们不是同时到达另一侧的，提交后一秒内在另一侧看到的可能是中间状态。

使用约定：

- 同一个仓库，同一时间只在一侧做 git 写操作（提交、切换分支、暂存、拉取）。只开着编辑器看不算。
- 推荐让不在工作的一侧只读：Mac 编辑器的后台 git 刷新使用 `GIT_OPTIONAL_LOCKS=0`（Cursor 默认如此）。这不是硬性要求，编辑器会写索引的情况也测过。正在工作的一侧不要设置，让它的 `status` 写回缓存，后续命令才快。
- 不需要 `core.checkStat`、`core.trustctime` 等设置。修改时间不保留时，它们起不到作用。

初步实测（2026-11-12，在 Mac 上用临时目录和一次性容器，测完已删除）：

| 实验 | 结果 |
| --- | --- |
| Mutagen 是否保留修改时间 | 不保留；另一侧运行一次 `git status` 后索引被改写 |
| 两侧轮流修改、提交、切换分支，共 3 轮 | 每一步之后两侧状态干净、提交一致，没有冲突 |
| Mac 侧每 0.5 秒在后台运行一次 `status`（只读和会写两种都测），另一侧连续 40 轮新建文件、修改、`status`、`add`、`commit`，中间不留间隔 | 两种都没有命令失败、冲突或暂存丢失；41 个提交两侧一致，`git fsck` 没有问题。这项两侧都在 Mac 上，没有经过容器 |
| 容器经 virtiofs 写文件 | 0.14 秒到达 Mac |
| 2 万个文件的仓库，换侧后第一次 `git status` | Mac 约 0.5 秒，容器约 0.5 到 0.8 秒；容器同一侧连续执行 37 ms |

还要在阶段 1 复核的：真实的 Cursor（除了 `status`，还有自动 `fetch` 等操作）和 Zed（它的 git 是否写索引还没查到）、大仓库的换侧耗时，以及容器连续提交时 Mac 编辑器的刷新开销。Mac 编辑器的后台 `status` 不写回缓存，所以容器提交之后，它每次刷新都要重读全部文件，直到 Mac 上运行一次会写索引的 git 命令。

不成立时的备选，都做过初步实测：

1. `.git` 不同步。Linux 树里每个仓库的 `.git` 是指向 Mac 侧 `.git` 的符号链接，容器另外把 Mac 的 workspace 挂到同一路径 `/Users/zeke/workspace`。git 的行为和旧方案相同，代价是每个仓库都要维护一个链接，并且容器能直接写 Mac 的 workspace。
2. 换用能保留修改时间的 Unison（`times` 选项），同步过来的索引在另一侧直接有效。需要另外安装，同时写的风险不变。

### 忽略名单

名单是这个设计里唯一的「规则」：声明哪些目录是每一侧私有的。

| 类别 | 条目 | 说明 |
| --- | --- | --- |
| 本部署的运行时状态 | `/github_work/cc-runtime/dev-runtimes/` `/github_work/cc-runtime/tailscale/state/` | 以同步根为锚的路径。家目录、SSH 主机密钥、machine-id 和 Tailscale 状态不进入副本 |
| 平台相关的依赖和构建产物 | `node_modules` `.venv` `target` `.next` `.nuxt` `.output` `.svelte-kit` `.turbo` `.parcel-cache` `.gradle` `.dart_tool` `.angular` `.tox` `.nox` | 任意深度按名字匹配 |
| 没有必要共享的缓存 | `__pycache__` `.pytest_cache` `.ruff_cache` `.mypy_cache` | 各自生成，同步只会增加噪音 |

不在名单里的：`bin`、`obj`、`build`、`dist`、`vendor`。这些名字经常就是源码目录，不能按名字全局忽略，默认照常同步；确有需要时为具体项目加带路径的条目。

### 检查脚本

`scripts/check-sync-ignores.py`（待写），在 Mac 侧运行，三个断言：

1. **两端互不包含**：会话的两个端点路径没有包含关系，并且忽略名单包含 `dev-runtimes/` 条目。
2. **没有误伤**：任何被 git 跟踪的文件都不落在忽略名单内。被 git 跟踪的一定是源码，所以这类错误可以确定性地查出来。
3. **没有明显遗漏**：未被忽略的目录里不应出现 `pyvenv.cfg`（即名字不标准的虚拟环境，如现有的 `.venv-mac`、`.vnev`），发现时给出警告。

### 状态可见性

- Mac 上：`mutagen sync list` 显示会话状态、未解决的冲突和问题。
- 容器里：通过已有的宿主机 SSH 执行同一条命令。
- 守护进程停止时两边会各自继续工作并逐渐分叉，所以启动检查里要包含「会话存在且在运行」。是否在 shell 提示符里显示同步状态，留到试点后决定。

Mutagen 在同步根被整个删除或清空时会暂停会话，而不是把删除传播到另一侧；文档说明这类保护是尽力而为的。

## 仓库里的变化

| 动作 | 对象 |
| --- | --- |
| 迁移 | `dev-california-runtime/` → `dev-runtimes/dev-CA/`，`dev-runtime/` → `dev-runtimes/dev-PA/`；Compose 服务 `dev-california` → `dev-CA`，`dev` → `dev-PA` |
| 删除 | `scripts/dev.py` 中的 `scan`、`make_plan`、`generate`、`initialize` 及相关命令；`dependency-rules.json`；`.runtime/compose.dependencies.json` 和 `.runtime/dependencies.json`；`tests/test_dependencies.py` 中的扫描与卷测试 |
| 保留 | `scripts/dev.py` 中的家目录、SSH 主机密钥、machine-id 初始化 |
| 修改 | `compose.yaml`（各机器的挂载源、静态的缓存卷）；`.gitignore` 和 `.dockerignore`（改为排除 `dev-runtimes/`）；`scripts/seed-dev-home.py` 和 `tests/test_identity.py` 中的路径；`README.md` 对应章节；`AGENTS.md` 中关于目录归组、依赖卷和 `scripts/dev.py` 的约定 |
| 新增 | 同步会话的定义、`scripts/check-sync-ignores.py` |

## 代价与已知限制

- **容器内依赖读写变慢。** 在本机 OrbStack 上用 2 万个 1 KB 文件做的合成测试：

  | 操作 | Linux 卷 | Mac 目录（virtiofs） |
  | --- | --- | --- |
  | 创建 | 0.3 s | 2.1 s |
  | 遍历并 stat | 0.03 s | 0.2 s |
  | 全部读取 | 0.07 s | 0.7 s |
  | 删除 | 0.13 s | 1.4 s |

  小文件操作慢 6 到 10 倍。预计影响集中在冷装依赖和大项目冷启动，量级是多几秒到十几秒。真实项目上的耗时还没有测过。
- **git 状态靠文件同步来复制。** 这不是 git 设计的用法。按「`.git` 的同步」里的约定轮流使用时，初步实测正常；两侧同时执行写操作的 git 命令仍会产生冲突，要人处理。换侧后的第一次 git 命令要重读全部文件。
- **多一个常驻组件。** Mac 上要运行 Mutagen 守护进程（已安装）。它仍是 1.0 之前的版本，搜索到的最近提交在 2026 年 4 月。
- **源码在 Mac 磁盘上存两份。** 三个主要目录排除依赖后约 33 万个文件，`.git` 合计约 2.5 GB。其余顶层目录没有量过。
- **冲突需要人处理。** 同一文件在同步窗口内被两边同时修改时会被标记，不会自动合并。
- **重命名或移动项目后，另一侧的依赖留在旧路径。** 需要在新路径重新安装，旧路径的残留要手动清理。
- **规则没有消失。** 仍然要维护一份忽略名单，只是从「启动时按标记文件预判每个目录」变成「十几个名字持续生效，并且有脚本校验」。

## 尚未验证

按重要性排列。第一项决定方案是否成立，在动任何容器之前先验证：

1. `.git` 随源码同步后，在真实编辑器和大仓库下 git 仍正常工作。初步实测已通过，待复核的项目见「`.git` 的同步」。
2. 两个方向的实际延迟，包括切换大分支这类一次改动上千个文件的操作。容器经 virtiofs 写入的文件能被及时感知，已初步实测（0.14 秒）。
3. 在一侧删除或重命名含有依赖目录的项目时，另一侧的表现。
4. 真实项目的依赖安装和构建耗时，与现在放在卷上的对比。

## 实施步骤

详细步骤、每一步的验收标准和回退方法见 [落地计划](dependency-isolation-rollout.md)。顺序是：

1. 在 Mac 上用临时目录验证同步工具，重点是它和 git 的配合。不通过就停下，回到设计。
2. `dev-california` 迁移为 `dev-CA`：目录、服务名，然后切换到同步的 workspace。`dev` 这段时间继续用旧方案。
3. `dev-CA` 全部验收通过后，`dev` 迁移为 `dev-PA`。
4. 清理：Mac 上由 Docker 创建的空挂载点目录、仓库里的扫描代码和规则文件、旧卷里的依赖子目录。

整个过程不改动 `/Users/zeke/workspace` 里项目的结构，旧卷保留到第 4 步，所以任何一步都可以退回旧方案。

如果依赖读写速度不可接受，可以只把同步的 beta 端换成 Docker 卷，忽略名单和检查脚本不变，代价见「设计原理」中的对比。

## 已定事项（2026-11-12）

- 所有顶层目录都参与同步。实测 `models` 3.8 GB、`rust_work` 11 GB，其余几个不到 20 MB，体积上没有必须排除的。
- `dev-CA` 的 `hostname` 保持 `dev-california`，它属于这台机器对外的身份。
- 本仓库的 `.env`、`proxy/config.yaml`、`proxy/config-california.yaml` 不从副本中排除，容器里仍可按 `AGENTS.md` 的流程同时修改派生配置。代价是容器仍能读到代理配置和凭据，和现状相同。
- `machine-id` 放在机器目录下，和 `home/` 同级。

## 待定事项

- 机器目录 `dev-runtimes/` 是否也移到 `~/.cc-runtime/` 下。移过去后同步根里不再有任何运行时状态，可以去掉对应的忽略条目；代价是这些状态不再和仓库放在一起。
- .NET 的 `bin`/`obj` 是否需要按项目加入忽略名单。Mac 侧目前没有这类项目的构建产物。

## 没有采用的方案

| 方案 | 不采用的原因 |
| --- | --- |
| 副本放 Docker 卷，同步进容器 | 见「设计原理」。保留为依赖速度不可接受时的后备 |
| 副本放在仓库的 `dev-runtimes/` 下 | 副本会位于同步根之内，正确性依赖一条忽略规则，见「副本放在 workspace 之外」 |
| 每台开发机各一份副本 | 开发机都是同一系统和架构，依赖没有差别；分开只会让源码份数、同步会话数和依赖安装次数都随机器数增加 |
| 工具级重定向（`UV_PROJECT_ENVIRONMENT`、`CARGO_TARGET_DIR` 等） | Node 没有等价机制，`node_modules` 必须同名并位于项目旁，无法覆盖全部 |
| 在容器内动态挂载 | 需要给开发容器 `SYS_ADMIN`，或用特权 sidecar 进入它的挂载命名空间；仍然是规则加挂载 |
| 自写按名字重定向的 FUSE 文件系统 | 要维护一个文件系统，所有文件操作变慢，需要额外权限 |
| 各自 checkout，靠 git 互通 | 未提交的改动互不可见，不符合「自然共享」的需求 |

## 参考

- [Mutagen 同步概览](https://mutagen.io/documentation/synchronization/)
- [Mutagen 文件监听](https://mutagen.io/documentation/synchronization/watching)
- [Mutagen 忽略规则](https://mutagen.io/documentation/synchronization/ignores)
- [Mutagen 安全机制](https://mutagen.io/documentation/synchronization/safety-mechanisms/)
- [Mutagen 对版本控制目录的建议](https://mutagen.io/documentation/synchronization/version-control-systems/)
- [Mutagen 符号链接](https://mutagen.io/documentation/synchronization/symbolic-links)、[权限](https://mutagen.io/documentation/synchronization/permissions)
