package gitstore_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/zeke-chin/cc-runtime/internal/gitstore"
	"github.com/zeke-chin/cc-runtime/internal/testutil"
)

const idle = 10 * time.Minute

type fixture struct {
	t                 *testing.T
	mac, linux, store string
	layout            gitstore.Layout
}

func setup(t *testing.T) *fixture {
	base := t.TempDir()
	f := &fixture{t: t, mac: filepath.Join(base, "mac"), linux: filepath.Join(base, "linux"), store: filepath.Join(base, "git")}
	for _, p := range []string{f.mac, f.linux, f.store} {
		os.Mkdir(p, 0o755)
	}
	f.layout = gitstore.Layout{Mac: f.mac, Linux: f.linux, Store: f.store, Ignores: testutil.ProjectIgnores()}
	return f
}

func (f *fixture) repo(root, rel string, commit bool) string {
	path := filepath.Join(root, rel)
	os.MkdirAll(path, 0o755)
	testutil.Git(f.t, path, "init", "-q")
	os.WriteFile(filepath.Join(path, "a.txt"), []byte("a\n"), 0o644)
	if commit {
		testutil.Git(f.t, path, "add", "a.txt")
		testutil.Git(f.t, path, "commit", "-qm", "init")
	}
	testutil.Age(f.t, filepath.Join(path, ".git"), idle)
	return path
}

// mirror 模拟同步会话：把源码和 .git 链接复制到另一棵树。
func (f *fixture) mirror(rel string) {
	for _, pair := range [][2]string{{f.mac, f.linux}, {f.linux, f.mac}} {
		src, dst := filepath.Join(pair[0], rel), filepath.Join(pair[1], rel)
		if _, err := os.Stat(src); err == nil && !exists(dst) {
			out, err := exec.Command("sh", "-c", `mkdir -p "$2" && cd "$1" && tar --exclude=./.git -cf - . | (cd "$2" && tar -xf -)`, "sh", src, dst).CombinedOutput()
			if err != nil {
				f.t.Fatal(string(out))
			}
			if gitstore.Kind(filepath.Join(src, ".git")) == "link" {
				target, _ := os.Readlink(filepath.Join(src, ".git"))
				os.Symlink(target, filepath.Join(dst, ".git"))
			}
		}
	}
}

func exists(path string) bool { _, err := os.Lstat(path); return err == nil }

func externalize(t *testing.T, l gitstore.Layout, rel string) (bool, string) {
	t.Helper()
	changed, message, err := gitstore.Externalize(l, rel, false)
	if err != nil {
		t.Fatal(err)
	}
	return changed, message
}

func TestExternalizeFromMacSharesGitDataWithLinux(t *testing.T) {
	f := setup(t)
	f.repo(f.mac, "g/foo", true)
	f.mirror("g/foo")
	if exists(filepath.Join(f.linux, "g/foo/.git")) {
		t.Fatal("真实的 .git 不应被复制")
	}
	if changed, message := externalize(t, f.layout, "g/foo"); !changed {
		t.Fatal(message)
	}
	link := filepath.Join(f.mac, "g/foo/.git")
	target, _ := os.Readlink(link)
	if target != filepath.Join(f.store, "g/foo.git") {
		t.Fatal(target)
	}
	os.Symlink(target, filepath.Join(f.linux, "g/foo/.git")) // 同步复制链接
	linuxRepo := filepath.Join(f.linux, "g/foo")
	real, _ := filepath.EvalSymlinks(linuxRepo)
	if got := strings.TrimSpace(testutil.Git(t, linuxRepo, "rev-parse", "--show-toplevel")); got != real {
		t.Fatal(got)
	}
	if got := testutil.Git(t, linuxRepo, "status", "--porcelain"); got != "" {
		t.Fatal(got)
	}
	os.WriteFile(filepath.Join(linuxRepo, "b.txt"), []byte("b\n"), 0o644)
	testutil.Git(t, linuxRepo, "add", "b.txt")
	testutil.Git(t, linuxRepo, "commit", "-qm", "from linux")
	if !strings.Contains(testutil.Git(t, filepath.Join(f.mac, "g/foo"), "log", "--oneline"), "from linux") {
		t.Fatal("Mac 侧看不到 Linux 侧的提交")
	}
	if anomalies := gitstore.Status(f.layout).Anomalies; len(anomalies) != 0 {
		t.Fatal(anomalies)
	}
}

func TestExternalizeRepositoryCreatedInLinuxTree(t *testing.T) {
	f := setup(t)
	f.repo(f.linux, "new", true)
	rel, err := f.layout.Rel(filepath.Join(f.linux, "new"))
	if err != nil {
		t.Fatal(err)
	}
	changed, message := externalize(t, f.layout, rel)
	if !changed || gitstore.Kind(filepath.Join(f.linux, "new/.git")) != "link" || !strings.Contains(message, "linux 目录树") {
		t.Fatal(message)
	}
}

func TestGuards(t *testing.T) {
	f := setup(t)
	expect := func(rel, fragment string) {
		t.Helper()
		if _, message := externalize(t, f.layout, rel); !strings.Contains(message, fragment) {
			t.Errorf("%s：%q 中没有 %q", rel, message, fragment)
		}
	}
	f.repo(f.mac, "both", true)
	f.repo(f.linux, "both", true)
	expect("both", "两棵目录树")
	busy := f.repo(f.mac, "busy", true)
	os.WriteFile(filepath.Join(busy, ".git/index.lock"), nil, 0o644)
	expect("busy", "锁文件")
	tool := f.repo(f.mac, "tool", true)
	os.MkdirAll(filepath.Join(tool, ".git/cursor/crepe"), 0o755)
	os.WriteFile(filepath.Join(tool, ".git/cursor/crepe/build.lock"), nil, 0o644) // 编辑器的文件，不是 git 的
	testutil.Age(t, filepath.Join(tool, ".git"), idle)
	if changed, message := externalize(t, f.layout, "tool"); !changed {
		t.Error(message)
	}
	reflock := f.repo(f.mac, "reflock", true)
	os.WriteFile(filepath.Join(reflock, ".git/refs/heads/main.lock"), nil, 0o644)
	testutil.Age(t, filepath.Join(reflock, ".git"), idle)
	expect("reflock", "refs/heads/main.lock")
	fresh := f.repo(f.mac, "fresh", true)
	os.WriteFile(filepath.Join(fresh, "a.txt"), []byte("changed\n"), 0o644)
	testutil.Git(t, fresh, "add", "a.txt")
	expect("fresh", "有改动")
	f.repo(f.mac, "empty", false)
	expect("empty", "还没有提交")
	f.repo(f.mac, "taken", true)
	os.Mkdir(filepath.Join(f.store, "taken.git"), 0o755)
	expect("taken", "已经存在")
	wt := f.repo(f.mac, "wt", true)
	testutil.Git(t, wt, "worktree", "add", "-q", "-b", "side", filepath.Join(f.mac, "wt-side"))
	testutil.Age(t, filepath.Join(wt, ".git"), idle)
	expect("wt", "worktree")
	lib := f.repo(f.mac, "lib", true)
	sup := f.repo(f.mac, "sup", true)
	testutil.Git(t, sup, "submodule", "add", "-q", lib, "lib")
	testutil.Git(t, sup, "commit", "-qm", "sub")
	testutil.Age(t, filepath.Join(sup, ".git"), idle)
	expect("sup", "子模块")
	entries, _ := os.ReadDir(f.store)
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if !slices.Equal(names, []string{"taken.git", "tool.git"}) {
		t.Fatal(names)
	}
}

func TestRestoreMovesDataBackAndRemovesBothLinks(t *testing.T) {
	f := setup(t)
	f.repo(f.mac, "g/foo", true)
	f.mirror("g/foo")
	externalize(t, f.layout, "g/foo")
	target, _ := os.Readlink(filepath.Join(f.mac, "g/foo/.git"))
	os.Symlink(target, filepath.Join(f.linux, "g/foo/.git"))
	testutil.Age(t, filepath.Join(f.store, "g/foo.git"), idle)
	changed, message, err := gitstore.Restore(f.layout, "g/foo", false)
	if err != nil || !changed {
		t.Fatal(message, err)
	}
	if gitstore.Kind(filepath.Join(f.mac, "g/foo/.git")) != "dir" || exists(filepath.Join(f.linux, "g/foo/.git")) {
		t.Fatal("没有恢复成真实目录，或 Linux 侧链接还在")
	}
	if entries, _ := os.ReadDir(f.store); len(entries) != 0 {
		t.Fatal("空的上级目录应被删除")
	}
	if got := testutil.Git(t, filepath.Join(f.mac, "g/foo"), "status", "--porcelain"); got != "" {
		t.Fatal(got)
	}
}

func TestStatusReportsCopyRenameDeleteAndDangling(t *testing.T) {
	f := setup(t)
	for _, rel := range []string{"copy", "moved", "gone", "dangling"} {
		f.repo(f.mac, rel, true)
		externalize(t, f.layout, rel)
	}
	if out, err := exec.Command("cp", "-R", filepath.Join(f.mac, "copy"), filepath.Join(f.mac, "copy2")).CombinedOutput(); err != nil {
		t.Fatal(string(out))
	}
	os.Rename(filepath.Join(f.mac, "moved"), filepath.Join(f.mac, "moved2"))
	os.RemoveAll(filepath.Join(f.mac, "gone"))
	os.RemoveAll(filepath.Join(f.store, "dangling.git"))
	text := strings.Join(gitstore.Status(f.layout).Anomalies, "\n")
	for _, fragment := range []string{"被多个工作树共用：copy, copy2", "moved2：链接目标", "gone.git：没有工作树链接到它", "dangling：链接目标"} {
		if !strings.Contains(text, fragment) {
			t.Errorf("缺少 %q：\n%s", fragment, text)
		}
	}
}

func TestScanSkipsIgnoredDirectories(t *testing.T) {
	f := setup(t)
	f.repo(f.mac, "app", true)
	f.repo(f.mac, "app/node_modules/dep", true)
	got := gitstore.ScanTree(f.mac, f.layout.Ignores)
	if len(got) != 1 || got["app"] != "dir" {
		t.Fatal(got)
	}
}
