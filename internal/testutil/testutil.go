// Package testutil 是各包测试共用的工具。
package testutil

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/zeke-chin/cc-runtime/internal/config"
)

const (
	MiB = 1 << 20
	Day = 24 * time.Hour
)

// RepoRoot 是本仓库的根目录。
func RepoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

// ProjectIgnores 是仓库里 sync-ignores 的条目。
func ProjectIgnores() config.IgnoreList {
	return config.NewIgnoreList(config.ReadList(filepath.Join(RepoRoot(), config.ProjectIgnoresFile)))
}

// MakeConfig 建一份工作区和运行目录都在 base 下的配置；root 为空时用 base/repo。
// 仓库目录里放一份真实的 sync-ignores。
func MakeConfig(t *testing.T, base, root string) config.Config {
	t.Helper()
	if root == "" {
		root = filepath.Join(base, "repo")
	}
	runtimeDir := filepath.Join(base, "runtime")
	for _, path := range []string{filepath.Join(base, "mac"), filepath.Join(runtimeDir, "linux-workspace"),
		filepath.Join(runtimeDir, "git"), root} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	data, err := os.ReadFile(filepath.Join(RepoRoot(), config.ProjectIgnoresFile))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, config.ProjectIgnoresFile), data, 0o644); err != nil {
		t.Fatal(err)
	}
	return config.Config{Root: root, Workspace: filepath.Join(base, "mac"), Runtime: runtimeDir,
		Downloads: filepath.Join(base, "downloads")}
}

// Git 在 dir 里运行 git，失败时让测试失败。
func Git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	full := append([]string{"-c", "user.name=t", "-c", "user.email=t@example.invalid",
		"-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"}, args...)
	cmd := exec.Command("git", full...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s：%v\n%s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

// Age 让 path 及其下的所有文件看起来闲置了一段时间（修改时间设在过去）。
func Age(t *testing.T, path string, ago time.Duration) {
	t.Helper()
	past := time.Now().Add(-ago)
	filepath.Walk(path, func(p string, info os.FileInfo, err error) error {
		if err == nil && info.Mode()&os.ModeSymlink == 0 {
			os.Chtimes(p, past, past)
		}
		return nil
	})
}

// Write 写文件并把修改时间设在 ago 之前。
func Write(t *testing.T, path string, data []byte, ago time.Duration) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
	past := time.Now().Add(-ago)
	os.Chtimes(path, past, past)
}
