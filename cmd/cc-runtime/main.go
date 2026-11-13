// cc-runtime 是开发运行环境的统一入口。用 scripts/install.sh 安装到 ~/.local/bin。
package main

import (
	"os"

	"github.com/zeke-chin/cc-runtime/internal/cli"
)

func main() {
	os.Exit(cli.Main(os.Args[1:]))
}
