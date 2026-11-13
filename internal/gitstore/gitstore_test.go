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
	// git 记下的是真实路径；macOS 的临时目录在 /var -> /private/var 之下。
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
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
	wtlock := f.repo(f.mac, "wtlock", true)
	testutil.Git(t, wtlock, "worktree", "add", "-q", "-b", "side", filepath.Join(wtlock, "wt"))
	os.WriteFile(filepath.Join(wtlock, ".git/worktrees/wt/index.lock"), nil, 0o644)
	testutil.Age(t, filepath.Join(wtlock, ".git"), idle)
	expect("wtlock", "worktrees/wt/index.lock")
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

// origin 在两棵树之外建一个有提交的仓库，供子模块使用。
func (f *fixture) origin(name string) string {
	path := filepath.Join(filepath.Dir(f.mac), "origin", name)
	os.MkdirAll(path, 0o755)
	testutil.Git(f.t, path, "init", "-q")
	os.WriteFile(filepath.Join(path, name), []byte(name+"\n"), 0o644)
	testutil.Git(f.t, path, "add", name)
	testutil.Git(f.t, path, "commit", "-qm", name)
	return path
}

// withLinuxAtWorkspacePath 把 Linux 树挪到工作区路径上运行 fn，模拟开发机：
// 开发机把 Linux 树挂在和 Mac 工作区相同的路径上。
func (f *fixture) withLinuxAtWorkspacePath(fn func()) {
	f.t.Helper()
	parked := f.mac + ".parked"
	if err := os.Rename(f.mac, parked); err != nil {
		f.t.Fatal(err)
	}
	if err := os.Rename(f.linux, f.mac); err != nil {
		f.t.Fatal(err)
	}
	defer func() {
		os.Rename(f.mac, f.linux)
		os.Rename(parked, f.mac)
	}()
	fn()
}

// sync 把 src 树的内容同步到 dst 树：真实的 .git 目录不同步，.git 链接和 .git 文件照常同步。
func sync(t *testing.T, src, dst string) {
	t.Helper()
	os.RemoveAll(dst)
	err := filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			link, _ := os.Readlink(path)
			return os.Symlink(link, target)
		case d.IsDir():
			if d.Name() == ".git" {
				return filepath.SkipDir
			}
			return os.MkdirAll(target, 0o755)
		default:
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			return os.WriteFile(target, data, info.Mode().Perm())
		}
	})
	if err != nil {
		t.Fatal(err)
	}
}

func noPrunable(t *testing.T, repo string) {
	t.Helper()
	if out := testutil.Git(t, repo, "worktree", "list"); strings.Contains(out, "prunable") {
		t.Fatal(out)
	}
	if out := testutil.Git(t, repo, "worktree", "prune", "-n", "-v"); out != "" {
		t.Fatal(out)
	}
}

func TestWorktreesAndSubmodulesWorkOnBothSides(t *testing.T) {
	f := setup(t)
	inner := f.origin("inner")
	lib := f.origin("lib")
	testutil.Git(t, lib, "submodule", "add", "-q", inner, "inner")
	testutil.Git(t, lib, "commit", "-qm", "nested")
	repo := f.repo(f.mac, "g/app", true)
	testutil.Git(t, repo, "submodule", "add", "-q", lib, "lib")
	testutil.Git(t, repo, "submodule", "update", "-q", "--init", "--recursive")
	testutil.Git(t, repo, "commit", "-qm", "sub")
	testutil.Git(t, repo, "worktree", "add", "-q", "-b", "in", ".agents/wt")
	testutil.Git(t, repo, "worktree", "add", "-q", "-b", "out", filepath.Join(f.mac, "g/app-side"))
	testutil.Git(t, repo, "worktree", "add", "-q", "--relative-paths", "-b", "rel", ".agents/rel")
	testutil.Age(t, filepath.Join(repo, ".git"), idle)

	changed, message := externalize(t, f.layout, "g/app")
	if !changed || !strings.Contains(message, "改写了") {
		t.Fatal(message)
	}
	check := func(side string) {
		t.Helper()
		noPrunable(t, repo)
		for _, dir := range []string{"lib", "lib/inner", ".agents/wt", ".agents/rel", "../app-side"} {
			if _, err := exec.Command("git", "-C", filepath.Join(repo, dir), "status", "-s").CombinedOutput(); err != nil {
				out, _ := exec.Command("git", "-C", filepath.Join(repo, dir), "status").CombinedOutput()
				t.Fatalf("%s：%s 里 git 不可用：%s", side, dir, out)
			}
		}
		if out := testutil.Git(t, repo, "submodule", "status", "--recursive"); strings.Count(out, "heads/") != 2 {
			t.Fatalf("%s：%s", side, out)
		}
	}
	check("mac")

	sync(t, f.mac, f.linux)
	f.withLinuxAtWorkspacePath(func() {
		check("linux")
		os.WriteFile(filepath.Join(repo, ".agents/wt/b.txt"), []byte("b\n"), 0o644)
		testutil.Git(t, filepath.Join(repo, ".agents/wt"), "add", "b.txt")
		testutil.Git(t, filepath.Join(repo, ".agents/wt"), "commit", "-qm", "from linux")
		testutil.Git(t, repo, "worktree", "add", "-q", "-b", "linux", ".agents/linux")
		testutil.Git(t, repo, "gc", "-q")
	})
	sync(t, f.linux, f.mac)
	check("mac")
	if !strings.Contains(testutil.Git(t, repo, "log", "--oneline", "in"), "from linux") {
		t.Fatal("Mac 侧看不到 Linux 侧在 worktree 里的提交")
	}
	testutil.Git(t, filepath.Join(repo, ".agents/linux"), "status")
	if report := gitstore.Status(f.layout); len(report.Anomalies)+len(report.Warnings) != 0 {
		t.Fatal(report.Anomalies, report.Warnings)
	}
}

func TestRepairRewritesPathsValidOnOneSideOnly(t *testing.T) {
	f := setup(t)
	legacy := filepath.Join(filepath.Dir(f.mac), "legacy") // 开发机以前的挂载点
	f.layout.Aliases = []string{legacy}
	repo := f.repo(f.mac, "app", true)
	testutil.Git(t, repo, "worktree", "add", "-q", "-b", "a", ".agents/a")
	testutil.Git(t, repo, "worktree", "add", "-q", "-b", "b", ".agents/b")
	testutil.Age(t, filepath.Join(repo, ".git"), idle)
	externalize(t, f.layout, "app")
	store := f.layout.Target("app")
	// a：在旧挂载点下建的；b：git 在 Mac 上直接操作过 Linux 树。
	os.WriteFile(filepath.Join(store, "worktrees/a/gitdir"), []byte(legacy+"/app/.agents/a/.git\n"), 0o644)
	os.WriteFile(filepath.Join(repo, ".agents/a/.git"), []byte("gitdir: "+legacy+"/app/.git/worktrees/a\n"), 0o644)
	os.WriteFile(filepath.Join(store, "worktrees/b/gitdir"), []byte(f.linux+"/app/.agents/b/.git\n"), 0o644)
	testutil.Age(t, store, idle)

	changed, message, err := gitstore.Repair(f.layout, "app")
	if err != nil || !changed {
		t.Fatal(message, err)
	}
	for _, name := range []string{"a", "b"} {
		data, _ := os.ReadFile(filepath.Join(store, "worktrees", name, "gitdir"))
		if got := strings.TrimSpace(string(data)); got != filepath.Join(repo, ".agents", name, ".git") {
			t.Errorf("%s：%s", name, got)
		}
	}
	noPrunable(t, repo)
	testutil.Git(t, filepath.Join(repo, ".agents/a"), "status")
	if changed, message, _ := gitstore.Repair(f.layout, "app"); changed {
		t.Fatal("第二次不应再改：" + message)
	}
}

func TestRestoreKeepsWorktreesAndSubmodulesWorking(t *testing.T) {
	f := setup(t)
	lib := f.origin("lib")
	repo := f.repo(f.mac, "app", true)
	testutil.Git(t, repo, "submodule", "add", "-q", lib, "lib")
	testutil.Git(t, repo, "commit", "-qm", "sub")
	testutil.Age(t, filepath.Join(repo, ".git"), idle)
	externalize(t, f.layout, "app")
	// 迁出期间用相对路径建的 worktree，相对路径穿过共享目录。
	testutil.Git(t, repo, "worktree", "add", "-q", "--relative-paths", "-b", "wt", ".agents/wt")
	testutil.Age(t, f.layout.Target("app"), idle)
	changed, message, err := gitstore.Restore(f.layout, "app", false)
	if err != nil || !changed {
		t.Fatal(message, err)
	}
	noPrunable(t, repo)
	testutil.Git(t, filepath.Join(repo, "lib"), "status")
	testutil.Git(t, filepath.Join(repo, ".agents/wt"), "status")
}

func TestStatusWarnsAboutWorktreeOutsideWorkspace(t *testing.T) {
	f := setup(t)
	repo := f.repo(f.mac, "app", true)
	outside := filepath.Join(filepath.Dir(f.mac), "elsewhere")
	testutil.Git(t, repo, "worktree", "add", "-q", "-b", "x", outside)
	testutil.Age(t, filepath.Join(repo, ".git"), idle)
	if changed, message := externalize(t, f.layout, "app"); !changed {
		t.Fatal(message)
	}
	testutil.Git(t, outside, "status") // Mac 侧仍可用
	report := gitstore.Status(f.layout)
	if text := strings.Join(report.Warnings, "\n"); !strings.Contains(text, "在工作区之外") || len(report.Anomalies) != 0 {
		t.Fatal(report.Warnings, report.Anomalies)
	}
}
