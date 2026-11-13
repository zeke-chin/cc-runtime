// Package config 读取路径和设置：来自环境变量和仓库的 .env 文件。
package config

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// DefaultRoot 是安装时写入二进制的仓库路径（scripts/install.sh 用 -ldflags 设置）。
var DefaultRoot string

// Version 是安装时写入的提交号。
var Version = "dev"

const (
	// ProjectIgnoresFile 是入库的同步忽略名单，所有部署共用。
	ProjectIgnoresFile = "sync-ignores"
	// LocalIgnoresFile 是只属于这台 Mac 的条目，不入库。
	LocalIgnoresFile = "sync-ignores.local"
	Session          = "linux-workspace"
)

// FindRoot 确定仓库目录：CC_RUNTIME_ROOT，其次是安装时写入的路径，最后从当前目录向上查找。
func FindRoot() (string, error) {
	if root := os.Getenv("CC_RUNTIME_ROOT"); root != "" {
		return root, nil
	}
	if DefaultRoot != "" && isRepo(DefaultRoot) {
		return DefaultRoot, nil
	}
	if dir, err := os.Getwd(); err == nil {
		for {
			if isRepo(dir) {
				return dir, nil
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	if DefaultRoot != "" {
		return "", errors.New("安装时记录的仓库 " + DefaultRoot + " 已不存在；请在仓库里重新运行 scripts/install.sh，或设置 CC_RUNTIME_ROOT")
	}
	return "", errors.New("找不到 cc-runtime 仓库：请在仓库里运行 scripts/install.sh 安装，或设置 CC_RUNTIME_ROOT")
}

func isRepo(dir string) bool {
	for _, name := range []string{"compose.yaml", "go.mod"} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			return false
		}
	}
	data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
	return err == nil && strings.Contains(string(data), "module github.com/zeke-chin/cc-runtime")
}

// ReadEnvFile 读取 KEY=VALUE 格式的文件；文件不存在时返回空表。
func ReadEnvFile(path string) map[string]string {
	values := map[string]string{}
	data, err := os.ReadFile(path)
	if err != nil {
		return values
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
			continue
		}
		key, value, _ := strings.Cut(line, "=")
		value = strings.TrimSpace(value)
		if len(value) >= 2 && value[0] == value[len(value)-1] && (value[0] == '\'' || value[0] == '"') {
			value = value[1 : len(value)-1]
		}
		values[strings.TrimSpace(key)] = value
	}
	return values
}

// ExpandPath 展开 ~ 和环境变量。
func ExpandPath(value string) string {
	if value == "~" || strings.HasPrefix(value, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			value = home + value[1:]
		}
	}
	return filepath.Clean(os.ExpandEnv(value))
}

// IgnoreList 是 Mutagen 风格的忽略名单：'/锚定/路径/'，或在任意深度匹配的名字。
type IgnoreList struct {
	Entries  []string
	anchored []string
	names    map[string]bool
}

func NewIgnoreList(entries []string) IgnoreList {
	list := IgnoreList{Entries: append([]string(nil), entries...), names: map[string]bool{}}
	for _, entry := range entries {
		if strings.HasPrefix(entry, "/") {
			if !strings.HasSuffix(entry, "/") {
				entry += "/"
			}
			list.anchored = append(list.anchored, entry)
		} else {
			list.names[strings.TrimRight(entry, "/")] = true
		}
	}
	return list
}

func (l IgnoreList) Matches(rel string) bool {
	var parts []string
	for _, part := range strings.Split(strings.Trim(rel, "/"), "/") {
		if part != "" {
			parts = append(parts, part)
			if l.names[part] {
				return true
			}
		}
	}
	path := "/" + strings.Join(parts, "/") + "/"
	for _, entry := range l.anchored {
		if strings.HasPrefix(path, entry) {
			return true
		}
	}
	return false
}

type Config struct {
	Root      string
	Workspace string
	Runtime   string
	Downloads string
	// Screenshots 是可选的宿主机截图目录（HOST_SCREENSHOTS_PATH），未配置时为空。
	Screenshots string
	Env         map[string]string
}

func (c Config) LinuxWorkspace() string { return filepath.Join(c.Runtime, "linux-workspace") }
func (c Config) GitStore() string       { return filepath.Join(c.Runtime, "git") }
func (c Config) StateDir() string       { return filepath.Join(c.Runtime, "state") }
func (c Config) LogDir() string         { return filepath.Join(c.Runtime, "logs") }
func (c Config) TmpDir() string         { return filepath.Join(c.Runtime, "tmp") }

func resolve(path string) string {
	if real, err := filepath.EvalSymlinks(path); err == nil {
		return real
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return abs
}

// RepoRel 是本仓库在工作区里的相对路径；不在工作区里时返回 false。
func (c Config) RepoRel() (string, bool) {
	rel, err := filepath.Rel(resolve(c.Workspace), resolve(c.Root))
	if err != nil || rel == ".." || strings.HasPrefix(rel, "../") {
		return "", false
	}
	return filepath.ToSlash(rel), true
}

// DeploymentIgnores 是本部署的每机状态，不进入 Linux 副本。
func (c Config) DeploymentIgnores() []string {
	rel, ok := c.RepoRel()
	if !ok {
		return nil
	}
	return []string{"/" + rel + "/dev-runtimes/", "/" + rel + "/tailscale/state/"}
}

// ReadList 读取每行一条的名单文件，去掉注释和空行；文件不存在时返回空。
func ReadList(path string) []string {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var entries []string
	for _, line := range strings.Split(string(data), "\n") {
		line, _, _ = strings.Cut(line, "#")
		if line = strings.TrimSpace(line); line != "" {
			entries = append(entries, line)
		}
	}
	return entries
}

func (c Config) ProjectIgnores() []string {
	return ReadList(filepath.Join(c.Root, ProjectIgnoresFile))
}

func (c Config) LocalIgnores() []string {
	return ReadList(filepath.Join(c.Root, LocalIgnoresFile))
}

func (c Config) Ignores() IgnoreList {
	seen := map[string]bool{}
	var entries []string
	all := append(append(c.DeploymentIgnores(), c.LocalIgnores()...), c.ProjectIgnores()...)
	for _, entry := range all {
		if !seen[entry] {
			seen[entry] = true
			entries = append(entries, entry)
		}
	}
	return NewIgnoreList(entries)
}

// Load 读取 root/.env，再用环境变量覆盖其中的目录设置。environ 为 nil 时使用进程环境。
func Load(root string, environ map[string]string) Config {
	if environ == nil {
		environ = map[string]string{}
		for _, item := range os.Environ() {
			key, value, _ := strings.Cut(item, "=")
			environ[key] = value
		}
	}
	values := ReadEnvFile(filepath.Join(root, ".env"))
	for _, key := range []string{"WORKSPACE_DIR", "RUNTIME_DIR", "DOWNLOADS_DIR", "HOST_SCREENSHOTS_PATH"} {
		if value, ok := environ[key]; ok {
			values[key] = value
		}
	}
	get := func(key, fallback string) string {
		if value, ok := values[key]; ok {
			return ExpandPath(value)
		}
		return ExpandPath(fallback)
	}
	cfg := Config{
		Root:      root,
		Workspace: get("WORKSPACE_DIR", "~/workspace"),
		Runtime:   get("RUNTIME_DIR", "~/.cc-runtime"),
		Downloads: get("DOWNLOADS_DIR", "~/Downloads"),
		Env:       values,
	}
	if value := strings.TrimSpace(values["HOST_SCREENSHOTS_PATH"]); value != "" {
		cfg.Screenshots = ExpandPath(value)
	}
	return cfg
}
