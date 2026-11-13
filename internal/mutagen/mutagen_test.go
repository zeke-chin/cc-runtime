package mutagen_test

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/zeke-chin/cc-runtime/internal/mutagen"
	"github.com/zeke-chin/cc-runtime/internal/testutil"
)

const sessionOutput = `--------------------------------------------------------------------------------
Name: linux-workspace
Configuration:
	Symbolic link mode: POSIX Raw
	Ignores:
		/github_work/cc-runtime/dev-runtimes/
		/github_work/cc-runtime/tailscale/state/
		/rust_work/.cache/
		node_modules
	Ignore VCS mode: Ignore
Alpha:
	URL: /Users/you/workspace
	Connected: Yes
Beta:
	URL: /Users/you/.cc-runtime/linux-workspace
	Connected: Yes
Conflicts:
	(alpha) app/a.txt (File (x) -> File (y))
	(beta)  app/a.txt (File (x) -> File (z))

Status: Watching for changes
--------------------------------------------------------------------------------
`

func TestParseAndDrift(t *testing.T) {
	state := mutagen.ParseSession(sessionOutput)
	if !state.Connected || state.Status != "Watching for changes" || state.Conflicts != 1 || state.Problems != 0 {
		t.Fatalf("%+v", state)
	}
	if !state.Healthy() || state.OK() { // 冲突需要处理
		t.Fatal("健康但不 OK")
	}
	if state.Ignores[0] != "/github_work/cc-runtime/dev-runtimes/" || len(state.Ignores) != 4 {
		t.Fatal(state.Ignores)
	}
	if mutagen.ParseSession(strings.Replace(sessionOutput, "Watching for changes", "[Paused]", 1)).Healthy() {
		t.Fatal("暂停的会话不健康")
	}
	base := t.TempDir()
	cfg := testutil.MakeConfig(t, base, filepath.Join(base, "mac", "github_work", "cc-runtime"))
	missing, extra := mutagen.IgnoreDrift(cfg, state)
	if !slices.Contains(missing, "target") || !slices.Equal(extra, []string{"/rust_work/.cache/"}) {
		t.Fatal(missing, extra)
	}
}

func TestCreateCommand(t *testing.T) {
	command := mutagen.CreateCommand(testutil.MakeConfig(t, t.TempDir(), ""), "", "", "", "")
	if !slices.Contains(command, "--ignore-vcs") || !slices.Contains(command, "--symlink-mode=posix-raw") ||
		slices.Contains(command, "--ignore=.git") || !slices.Contains(command, "--ignore=node_modules") {
		t.Fatal(command)
	}
}

func TestCompareTrees(t *testing.T) {
	cfg := testutil.MakeConfig(t, t.TempDir(), "")
	mac, linux := cfg.Workspace, cfg.LinuxWorkspace()
	ignores := testutil.ProjectIgnores()
	for _, root := range []string{mac, linux} {
		os.MkdirAll(filepath.Join(root, "p/node_modules"), 0o755)
		os.WriteFile(filepath.Join(root, "p/a.txt"), []byte("same\n"), 0o644)
		os.Symlink("a.txt", filepath.Join(root, "p/l"))
	}
	os.WriteFile(filepath.Join(mac, "p/node_modules/x"), []byte("mac only, ignored\n"), 0o644)
	os.Mkdir(filepath.Join(mac, "p/.git"), 0o755) // 真实的 .git 目录从不同步
	if diff, err := mutagen.Compare(mac, linux, ignores, true); err != nil || len(diff) != 0 {
		t.Fatal(diff, err)
	}
	os.WriteFile(filepath.Join(linux, "p/a.txt"), []byte("diff\n"), 0o644)
	os.WriteFile(filepath.Join(linux, "p/b.txt"), []byte("b\n"), 0o644)
	diff, err := mutagen.Compare(mac, linux, ignores, true)
	want := []string{"仅在 " + linux + "：p/b.txt", "内容不同：p/a.txt"}
	if err != nil || !slices.Equal(diff, want) {
		t.Fatal(diff, err)
	}
}
