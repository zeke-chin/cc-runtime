package config_test

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/testutil"
)

func TestEnvFileAndEnvironment(t *testing.T) {
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, ".env"), []byte("# comment\nPASSWORD=x\nWORKSPACE_DIR=\"/w s\"\nRUNTIME_DIR=/r\n"), 0o644)
	cfg := config.Load(root, map[string]string{"RUNTIME_DIR": "/override"})
	if cfg.Workspace != "/w s" || cfg.Runtime != "/override" {
		t.Fatalf("得到 %q %q", cfg.Workspace, cfg.Runtime)
	}
	if cfg.LinuxWorkspace() != "/override/linux-workspace" || cfg.GitStore() != "/override/git" {
		t.Fatal(cfg.LinuxWorkspace(), cfg.GitStore())
	}
	home, _ := os.UserHomeDir()
	if got := config.Load(filepath.Join(root, "missing"), map[string]string{}).Runtime; got != filepath.Join(home, ".cc-runtime") {
		t.Fatal(got)
	}
}

func TestScreenshotsIsOptional(t *testing.T) {
	root := t.TempDir()
	if got := config.Load(root, map[string]string{}).Screenshots; got != "" {
		t.Fatalf("未配置时得到 %q", got)
	}
	os.WriteFile(filepath.Join(root, ".env"), []byte("HOST_SCREENSHOTS_PATH=  \n"), 0o644)
	if got := config.Load(root, map[string]string{}).Screenshots; got != "" {
		t.Fatalf("空值得到 %q", got)
	}
	home, _ := os.UserHomeDir()
	got := config.Load(root, map[string]string{"HOST_SCREENSHOTS_PATH": "~/Pictures/shots"}).Screenshots
	if got != filepath.Join(home, "Pictures/shots") {
		t.Fatal(got)
	}
}

func TestIgnoreListComposition(t *testing.T) {
	base := t.TempDir()
	repo := filepath.Join(base, "mac", "github_work", "cc-runtime")
	cfg := testutil.MakeConfig(t, base, repo)
	os.WriteFile(filepath.Join(repo, config.LocalIgnoresFile), []byte("# local\n/big/data/  # comment\nnode_modules\n"), 0o644)
	entries := cfg.Ignores().Entries
	want := []string{"/github_work/cc-runtime/dev-runtimes/", "/github_work/cc-runtime/tailscale/state/", "/big/data/"}
	if !slices.Equal(entries[:3], want) {
		t.Fatal(entries[:3])
	}
	count := 0
	for _, e := range entries {
		if e == "node_modules" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("node_modules 出现 %d 次", count)
	}
	if !slices.Contains(entries, "target") {
		t.Fatal("没有读到仓库里的 sync-ignores")
	}
	// 仓库不在工作区里时没有锚定条目。
	if got := testutil.MakeConfig(t, base, filepath.Join(base, "elsewhere")).DeploymentIgnores(); len(got) != 0 {
		t.Fatal(got)
	}
}

func TestMissingProjectFileMeansNoNameIgnores(t *testing.T) {
	cfg := config.Config{Root: t.TempDir(), Workspace: t.TempDir()}
	if cfg.Ignores().Matches("app/node_modules") {
		t.Fatal("没有 sync-ignores 时不应有内置名字")
	}
}

func TestMatching(t *testing.T) {
	ignores := config.NewIgnoreList([]string{"/rust_work/.cache/", "/a/b", "node_modules"})
	for rel, want := range map[string]bool{
		"x/node_modules/y.js": true, "rust_work/.cache": true, "a/b/c": true,
		"other/rust_work/.cache": false, "x/node_modules_backup": false, "a/bc": false,
	} {
		if ignores.Matches(rel) != want {
			t.Errorf("Matches(%q) 应为 %v", rel, want)
		}
	}
}
