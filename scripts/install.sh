#!/bin/sh
# 编译 cc-runtime 并安装到 ~/.local/bin（可用 PREFIX 改目录）。
# 二进制里记录本仓库的路径和提交号；launchd 任务直接调用它，不依赖 Go 或仓库里的任何环境。
# 更新代码后重新运行本脚本即可，已安装的定时任务不用重装。
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prefix=${PREFIX:-$HOME/.local/bin}
command -v go >/dev/null 2>&1 || { echo "需要 Go（https://go.dev/dl/）" >&2; exit 1; }

version=$(git -C "$root" describe --always --dirty 2>/dev/null || echo unknown)
pkg=github.com/zeke-chin/cc-runtime/internal/config
mkdir -p "$prefix"
temporary="$prefix/.cc-runtime.$$"
trap 'rm -f "$temporary"' EXIT

cd "$root"
go build -trimpath -ldflags "-s -w -X $pkg.DefaultRoot=$root -X $pkg.Version=$version" \
    -o "$temporary" ./cmd/cc-runtime
# 先写临时文件再改名：正在运行的定时任务不受影响。
mv -f "$temporary" "$prefix/cc-runtime"
echo "已安装 ${prefix}/cc-runtime（${version}，仓库 ${root}）"
case ":$PATH:" in
    *":$prefix:"*) ;;
    *) echo "提示：${prefix} 不在 PATH 中" >&2 ;;
esac
