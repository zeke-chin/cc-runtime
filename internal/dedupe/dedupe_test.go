package dedupe

import (
	"bytes"
	"crypto/rand"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/testutil"
)

const MIB = testutil.MiB

func random(n int) []byte {
	b := make([]byte, n)
	rand.Read(b)
	return b
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

// copyStrategy 用普通复制代替克隆，适用于不支持克隆的文件系统。
func copyStrategy(tmpdir string) *CloneReplace { return &CloneReplace{TmpDir: tmpdir, Clone: copyFile} }

type fixture struct {
	t         *testing.T
	a, b, tmp string
	strategy  Strategy
}

func setup(t *testing.T) *fixture {
	base := t.TempDir()
	f := &fixture{t: t, a: filepath.Join(base, "mac"), b: filepath.Join(base, "linux"), tmp: filepath.Join(base, "work")}
	for _, p := range []string{f.a, f.b, f.tmp} {
		os.Mkdir(p, 0o755)
	}
	f.strategy = copyStrategy(f.tmp)
	return f
}

func (f *fixture) both(rel string, data []byte, beta []byte) {
	if beta == nil {
		beta = data
	}
	testutil.Write(f.t, filepath.Join(f.a, rel), data, 2*testutil.Day)
	testutil.Write(f.t, filepath.Join(f.b, rel), beta, 2*testutil.Day)
}

// run 去重一次。ctime 无法往回设，除非测试需要，否则把它当作“现在”。
func (f *fixture) run(state State, opts Options) (Stats, State) {
	if state == nil {
		state = State{}
	}
	if opts.MinSize == 0 {
		opts.MinSize = MIB
	}
	stats, newState, err := Deduplicate(f.a, f.b, f.strategy, state, testutil.ProjectIgnores(), opts)
	if err != nil {
		f.t.Fatal(err)
	}
	return stats, newState
}

func keys(s State) []string {
	var out []string
	for k := range s {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}

func TestSelectsOnlyEligiblePairs(t *testing.T) {
	f := setup(t)
	big := random(2 * MIB)
	f.both("p/data.bin", big, nil)
	f.both("p/small.txt", []byte("tiny"), nil)
	f.both("p/node_modules/dep.bin", big, nil) // 被忽略：从不检查
	f.both("p/changed.bin", big, random(2*MIB))
	f.both("p/size.bin", big, append(append([]byte(nil), big...), 'x'))
	testutil.Write(t, filepath.Join(f.b, "p/only-linux.bin"), big, 2*testutil.Day)
	stats, state := f.run(nil, Options{})
	if stats.Shared != 1 || stats.Differs != 1 || stats.SizeDiffers != 1 || stats.Missing != 1 || stats.Small != 1 {
		t.Fatalf("%+v", stats)
	}
	if !slices.Equal(keys(state), []string{"p/data.bin"}) {
		t.Fatal(keys(state))
	}
	if data, _ := os.ReadFile(filepath.Join(f.b, "p/data.bin")); !bytes.Equal(data, big) {
		t.Fatal("内容变了")
	}
}

func TestRecentlyChangedFilesWait(t *testing.T) {
	f := setup(t)
	f.both("p/data.bin", random(2*MIB), nil)
	stats, _ := f.run(nil, Options{QuietSeconds: 86400}) // ctime 是现在：太新
	if stats.Recent != 1 || stats.Shared != 0 {
		t.Fatalf("%+v", stats)
	}
	stats, _ = f.run(nil, Options{QuietSeconds: 86400, Now: time.Now().Add(2 * testutil.Day)})
	if stats.Shared != 1 {
		t.Fatalf("%+v", stats)
	}
}

func TestStateSkipsAlreadySharedPairsAndNoticesChanges(t *testing.T) {
	f := setup(t)
	f.both("p/data.bin", random(2*MIB), nil)
	_, state := f.run(nil, Options{})
	stats, state := f.run(state, Options{})
	if stats.Already != 1 || stats.Shared != 0 {
		t.Fatalf("%+v", stats)
	}
	testutil.Write(t, filepath.Join(f.a, "p/data.bin"), random(2*MIB), 2*testutil.Day) // Mac 侧改了
	stats, _ = f.run(state, Options{})
	if stats.Already != 0 || stats.Differs != 1 {
		t.Fatalf("%+v", stats)
	}
}

func TestDryRunChangesNothing(t *testing.T) {
	f := setup(t)
	f.both("p/data.bin", random(2*MIB), nil)
	before, _ := Lstat(filepath.Join(f.b, "p/data.bin"))
	stats, state := f.run(nil, Options{DryRun: true})
	if stats.WouldShare != 1 || stats.WouldShareBytes != 2*MIB || len(state) != 0 {
		t.Fatalf("%+v %v", stats, state)
	}
	if after, _ := Lstat(filepath.Join(f.b, "p/data.bin")); after.Ino != before.Ino {
		t.Fatal("dry run 替换了文件")
	}
}

func TestKeepsLinuxModeAndMtime(t *testing.T) {
	f := setup(t)
	f.both("p/tool.bin", random(2*MIB), nil)
	os.Chmod(filepath.Join(f.b, "p/tool.bin"), 0o755)
	os.Chmod(filepath.Join(f.a, "p/tool.bin"), 0o600)
	stamp := time.Unix(1_700_000_000, 0)
	os.Chtimes(filepath.Join(f.b, "p/tool.bin"), stamp, stamp)
	f.run(nil, Options{})
	info, _ := os.Stat(filepath.Join(f.b, "p/tool.bin"))
	if info.Mode().Perm() != 0o755 || info.ModTime().Unix() != 1_700_000_000 {
		t.Fatal(info.Mode(), info.ModTime())
	}
	if mac, _ := os.Stat(filepath.Join(f.a, "p/tool.bin")); mac.Mode().Perm() != 0o600 {
		t.Fatal("Mac 侧被改动了")
	}
}

func TestGivesUpWhenAFileChangesDuringTheClone(t *testing.T) {
	f := setup(t)
	data := random(2 * MIB)
	f.both("p/data.bin", data, nil)
	target := filepath.Join(f.b, "p/data.bin")
	f.strategy = &CloneReplace{TmpDir: f.tmp, Clone: func(src, dst string) error {
		if err := copyFile(src, dst); err != nil {
			return err
		}
		out, _ := os.OpenFile(target, os.O_APPEND|os.O_WRONLY, 0) // 同时有人写 Linux 副本
		defer out.Close()
		_, err := out.Write([]byte("new"))
		return err
	}}
	stats, state := f.run(nil, Options{})
	if stats.Changed != 1 || stats.Shared != 0 || len(state) != 0 {
		t.Fatalf("%+v", stats)
	}
	if got, _ := os.ReadFile(target); !bytes.Equal(got, append(data, []byte("new")...)) {
		t.Fatal("写入丢失")
	}
	if entries, _ := os.ReadDir(f.tmp); len(entries) != 0 {
		t.Fatal("留下了临时文件")
	}
}

func TestGitDirectoriesAreSkipped(t *testing.T) {
	f := setup(t)
	f.both("p/.git/objects/pack/big.pack", random(2*MIB), nil)
	if stats, _ := f.run(nil, Options{}); stats.Examined != 0 {
		t.Fatalf("%+v", stats)
	}
}

type noClones struct{ *CloneReplace }

func (noClones) Probe() error { return &Unsupported{"cannot clone files: Operation not supported"} } // ext4、NTFS 的情况

func TestUnsupportedFilesystemChangesNothing(t *testing.T) {
	cfg := testutil.MakeConfig(t, t.TempDir(), "")
	data := bytes.Repeat([]byte("x"), 2*MIB)
	testutil.Write(t, filepath.Join(cfg.Workspace, "x.bin"), data, 2*testutil.Day)
	testutil.Write(t, filepath.Join(cfg.LinuxWorkspace(), "x.bin"), data, 2*testutil.Day)
	before, _ := Lstat(filepath.Join(cfg.LinuxWorkspace(), "x.bin"))
	_, err := Run(cfg, RunOptions{Strategy: noClones{copyStrategy(cfg.TmpDir())}, MinSize: MIB})
	var unsupported *Unsupported
	if !errors.As(err, &unsupported) {
		t.Fatal(err)
	}
	if after, _ := Lstat(filepath.Join(cfg.LinuxWorkspace(), "x.bin")); after.Ino != before.Ino {
		t.Fatal("文件被替换了")
	}
	if _, err := os.Stat(filepath.Join(cfg.StateDir(), "dedupe.json")); err == nil {
		t.Fatal("不应写状态")
	}
}

func TestRunRecordsState(t *testing.T) {
	cfg := testutil.MakeConfig(t, t.TempDir(), "")
	data := random(2 * MIB)
	testutil.Write(t, filepath.Join(cfg.Workspace, "d.bin"), data, 2*testutil.Day)
	testutil.Write(t, filepath.Join(cfg.LinuxWorkspace(), "d.bin"), data, 2*testutil.Day)
	stats, err := Run(cfg, RunOptions{MinSize: MIB, Strategy: copyStrategy(filepath.Join(cfg.TmpDir(), "dedupe"))})
	if err != nil || stats.Shared != 1 {
		t.Fatal(stats, err)
	}
	if _, ok := LoadState(filepath.Join(cfg.StateDir(), "dedupe.json"))["d.bin"]; !ok {
		t.Fatal("状态里没有 d.bin")
	}
}

func TestStateFileFromPythonVersionIsReadable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "dedupe.json")
	os.WriteFile(path, []byte(`{"a/b.bin":[12,2097152,1700000000123456789,34,2097152,1700000000123456789]}`), 0o644)
	if got := LoadState(path)["a/b.bin"]; len(got) != 6 || got[2] != 1700000000123456789 {
		t.Fatal(got)
	}
}

// dedupeCapableDir 找一个支持 FIDEDUPERANGE 的目录（如 Btrfs）。
func dedupeCapableDir(t *testing.T) string {
	home, _ := os.UserHomeDir()
	for _, base := range []string{os.Getenv("CC_DEDUPE_TEST_DIR"), filepath.Join(home, ".cache"), os.TempDir()} {
		if base == "" {
			continue
		}
		dir, err := os.MkdirTemp(base, "cc-dedupe-test-")
		if err != nil {
			continue
		}
		if NewKernelDedupe(dir).Probe() == nil {
			return dir
		}
		os.RemoveAll(dir)
	}
	return ""
}

func extents(path string) []string {
	out, err := exec.Command("filefrag", "-v", path).Output()
	if err != nil {
		return nil
	}
	var starts []string
	for _, line := range strings.Split(string(out), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 3 && strings.Contains(line, ":") && fields[0][0] >= '0' && fields[0][0] <= '9' {
			starts = append(starts, fields[3])
		}
	}
	return starts
}

func TestKernelDedupeSharesIdenticalFilesAndRefusesDifferentOnes(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("Linux 内核接口")
	}
	dir := dedupeCapableDir(t)
	if dir == "" {
		t.Skip("没有支持 FIDEDUPERANGE 的文件系统")
	}
	defer os.RemoveAll(dir)
	a, b, c := filepath.Join(dir, "a"), filepath.Join(dir, "b"), filepath.Join(dir, "c")
	data := random(40 * MIB) // 超过单次 16 MiB 的限制
	os.WriteFile(a, data, 0o644)
	os.WriteFile(b, data, 0o644)
	changed := append(append([]byte(nil), data[:len(data)-1]...), '!')
	os.WriteFile(c, changed, 0o644)
	exec.Command("sync").Run()
	if ok, err := DedupeRange(a, b, int64(len(data))); !ok || err != nil {
		t.Fatal(ok, err)
	}
	if ok, err := DedupeRange(a, c, int64(len(data))); ok || err != nil {
		t.Fatal(ok, err)
	}
	if got, _ := os.ReadFile(b); !bytes.Equal(got, data) {
		t.Fatal("b 的内容变了")
	}
	exec.Command("sync").Run()
	if ea := extents(a); ea != nil && !slices.Equal(ea, extents(b)) {
		t.Fatal("没有共用物理块")
	}
}

func TestCloneReplacesLinuxCopyWithIdenticalContent(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("macOS clonefile")
	}
	base := t.TempDir()
	for _, name := range []string{"mac", "linux", "work"} {
		os.Mkdir(filepath.Join(base, name), 0o755)
	}
	data := random(8 * MIB)
	testutil.Write(t, filepath.Join(base, "mac/d.bin"), data, 2*testutil.Day)
	testutil.Write(t, filepath.Join(base, "linux/d.bin"), data, 2*testutil.Day)
	os.Chmod(filepath.Join(base, "linux/d.bin"), 0o640)
	before, _ := Lstat(filepath.Join(base, "linux/d.bin"))
	strategy := NewCloneReplace(filepath.Join(base, "work"))
	if err := strategy.Probe(); err != nil {
		t.Fatal(err)
	}
	stats, _, err := Deduplicate(filepath.Join(base, "mac"), filepath.Join(base, "linux"), strategy, State{},
		config.NewIgnoreList(nil), Options{MinSize: MIB})
	after, _ := Lstat(filepath.Join(base, "linux/d.bin"))
	if err != nil || stats.Shared != 1 || after.Ino == before.Ino || after.Mode&0o777 != 0o640 || after.Mtime != before.Mtime {
		t.Fatalf("%+v %+v %+v %v", stats, before, after, err)
	}
	if got, _ := os.ReadFile(filepath.Join(base, "linux/d.bin")); !bytes.Equal(got, data) {
		t.Fatal("内容变了")
	}
	if entries, _ := os.ReadDir(filepath.Join(base, "work")); len(entries) != 0 {
		t.Fatal("留下了临时文件")
	}
}
