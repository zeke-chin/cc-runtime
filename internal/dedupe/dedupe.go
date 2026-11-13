// Package dedupe 让 Mac 工作区和它的 Linux 副本共用磁盘数据块。
//
// Mutagen 把同步的每个文件写成新数据，两棵树占两份空间。两侧同一相对路径、
// 内容相同的文件改为共用 Mac 侧的数据块（文件系统克隆）。内容不变，只改动 Linux 侧。
//
//	macOS（APFS）：clonefile() 到同步树之外的临时文件，再次核对两侧文件，然后改名覆盖 Linux 副本。
//	Linux（Btrfs、XFS、bcachefs、ZFS 2.2+）：FIDEDUPERANGE；内核加锁比较，相同才共享。
package dedupe

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"golang.org/x/sys/unix"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/mutagen"
	"github.com/zeke-chin/cc-runtime/internal/util"
)

const MiB = 1 << 20

// Unsupported 表示这个文件系统或平台做不了去重；此时不做任何改动。
type Unsupported struct{ Msg string }

func (e *Unsupported) Error() string { return e.Msg }

// Stat 是 lstat 的结果里去重关心的部分。
type Stat struct {
	Dev, Ino     uint64
	Mode         uint32
	Size         int64
	Atime, Mtime int64 // 纳秒
	Ctime        int64
	Regular      bool
}

func Lstat(path string) (Stat, error) {
	var st unix.Stat_t
	if err := unix.Lstat(path, &st); err != nil {
		return Stat{}, &fs.PathError{Op: "lstat", Path: path, Err: err}
	}
	return Stat{
		Dev: uint64(st.Dev), Ino: st.Ino, Mode: uint32(st.Mode), Size: st.Size,
		Atime: st.Atim.Nano(), Mtime: st.Mtim.Nano(), Ctime: st.Ctim.Nano(),
		Regular: uint32(st.Mode)&unix.S_IFMT == unix.S_IFREG,
	}, nil
}

// identity 是从核对到替换之间不能变的东西。
func identity(st Stat) [4]int64 { return [4]int64{int64(st.Ino), st.Size, st.Mtime, st.Ctime} }

func stateKey(a, b Stat) []int64 {
	return []int64{int64(a.Ino), a.Size, a.Mtime, int64(b.Ino), b.Size, b.Mtime}
}

// Strategy 是一种共享数据块的方式。Apply 返回 "shared"、"differs" 或 "changed"。
type Strategy interface {
	Name() string
	Probe() error
	Apply(alpha, beta string, alphaSt, betaSt Stat) (string, error)
}

// CloneReplace（macOS）：克隆 Mac 文件，再改名覆盖 Linux 副本。
type CloneReplace struct {
	TmpDir  string
	Clone   func(src, dst string) error
	counter int
}

func NewCloneReplace(tmpdir string) *CloneReplace {
	return &CloneReplace{TmpDir: tmpdir, Clone: clonefile}
}

func (c *CloneReplace) Name() string { return "clonefile" }

func (c *CloneReplace) Probe() error {
	a, b := filepath.Join(c.TmpDir, "probe-a"), filepath.Join(c.TmpDir, "probe-b")
	os.Remove(a)
	os.Remove(b)
	defer os.Remove(a)
	defer os.Remove(b)
	if err := os.WriteFile(a, make([]byte, 65536), 0o644); err != nil {
		return &Unsupported{fmt.Sprintf("无法在 %s 中克隆文件：%v", c.TmpDir, err)}
	}
	if err := c.Clone(a, b); err != nil {
		return &Unsupported{fmt.Sprintf("无法在 %s 中克隆文件：%v", c.TmpDir, err)}
	}
	return nil
}

func (c *CloneReplace) Apply(alpha, beta string, alphaSt, betaSt Stat) (string, error) {
	same, err := mutagen.SameContent(alpha, beta)
	if err != nil {
		return "", err
	}
	if !same {
		return "differs", nil
	}
	c.counter++
	temporary := filepath.Join(c.TmpDir, fmt.Sprintf("%d-%d", os.Getpid(), c.counter))
	defer func() {
		if util.Lexists(temporary) {
			os.Remove(temporary)
		}
	}()
	if err := c.Clone(alpha, temporary); err != nil {
		return "", err
	}
	// 保留 Linux 副本自己的权限和时间：容器里的 git 会缓存它们。
	if err := os.Chmod(temporary, fs.FileMode(betaSt.Mode&0o777)|modeBits(betaSt.Mode)); err != nil {
		return "", err
	}
	if err := os.Chtimes(temporary, time.Unix(0, betaSt.Atime), time.Unix(0, betaSt.Mtime)); err != nil {
		return "", err
	}
	nowAlpha, err := Lstat(alpha)
	if err != nil {
		return "", err
	}
	nowBeta, err := Lstat(beta)
	if err != nil {
		return "", err
	}
	if identity(nowAlpha) != identity(alphaSt) || identity(nowBeta) != identity(betaSt) {
		return "changed", nil
	}
	if err := os.Rename(temporary, beta); err != nil {
		return "", err
	}
	return "shared", nil
}

func modeBits(mode uint32) fs.FileMode {
	var m fs.FileMode
	if mode&unix.S_ISUID != 0 {
		m |= fs.ModeSetuid
	}
	if mode&unix.S_ISGID != 0 {
		m |= fs.ModeSetgid
	}
	if mode&unix.S_ISVTX != 0 {
		m |= fs.ModeSticky
	}
	return m
}

// KernelDedupe（Linux）：FIDEDUPERANGE；不替换文件，由内核核对内容。
type KernelDedupe struct {
	TmpDir string
	Dedupe func(src, dst string, length int64) (bool, error)
}

func NewKernelDedupe(tmpdir string) *KernelDedupe {
	return &KernelDedupe{TmpDir: tmpdir, Dedupe: DedupeRange}
}

func (k *KernelDedupe) Name() string { return "FIDEDUPERANGE" }

func (k *KernelDedupe) Probe() error {
	a, b := filepath.Join(k.TmpDir, "probe-a"), filepath.Join(k.TmpDir, "probe-b")
	defer os.Remove(a)
	defer os.Remove(b)
	data := make([]byte, 65536)
	for i := range data {
		data[i] = 'x'
	}
	err := os.WriteFile(a, data, 0o644)
	if err == nil {
		err = os.WriteFile(b, data, 0o644)
	}
	if err == nil {
		_, err = k.Dedupe(a, b, int64(len(data)))
	}
	if err != nil {
		return &Unsupported{fmt.Sprintf("无法在 %s 中去重文件：%v", k.TmpDir, err)}
	}
	return nil
}

func (k *KernelDedupe) Apply(alpha, beta string, alphaSt, betaSt Stat) (string, error) {
	shared, err := k.Dedupe(alpha, beta, alphaSt.Size)
	if err != nil {
		return "", err
	}
	if shared {
		return "shared", nil
	}
	return "differs", nil
}

func DefaultStrategy(tmpdir string) (Strategy, error) {
	switch runtime.GOOS {
	case "darwin":
		return NewCloneReplace(tmpdir), nil
	case "linux":
		return NewKernelDedupe(tmpdir), nil
	}
	return nil, &Unsupported{"不支持的平台：" + runtime.GOOS}
}

// Stats 是一次去重的统计；JSON 键与定时任务记录、状态面板一致。
type Stats struct {
	Examined        int64    `json:"examined"`
	Shared          int64    `json:"shared"`
	SharedBytes     int64    `json:"shared_bytes"`
	Already         int64    `json:"already"`
	AlreadyBytes    int64    `json:"already_bytes"`
	Small           int64    `json:"small"`
	Missing         int64    `json:"missing"`
	SizeDiffers     int64    `json:"size_differs"`
	Recent          int64    `json:"recent"`
	Differs         int64    `json:"differs"`
	Changed         int64    `json:"changed"`
	Errors          int64    `json:"errors"`
	WouldShare      int64    `json:"would_share"`
	WouldShareBytes int64    `json:"would_share_bytes"`
	Method          string   `json:"method,omitempty"`
	DryRun          bool     `json:"dry_run"`
	Seconds         float64  `json:"seconds"`
	FreeGainedBytes int64    `json:"free_gained_bytes"`
	ErrorSamples    []string `json:"error_samples,omitempty"`
}

type State map[string][]int64

// candidates 列出 Linux 树里忽略名单之外的普通文件（相对路径）。
func candidates(betaRoot string, ignores config.IgnoreList, fn func(rel string)) error {
	return util.Walk(betaRoot, func(rel string, d fs.DirEntry) error {
		if rel == "" {
			return nil
		}
		if d.IsDir() {
			if d.Name() == ".git" || ignores.Matches(rel) {
				return fs.SkipDir
			}
			return nil
		}
		if d.Name() == ".git" || ignores.Matches(rel) {
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 && util.IsDir(filepath.Join(betaRoot, rel)) {
			return nil // 指向目录的链接：不是文件
		}
		fn(rel)
		return nil
	})
}

type Options struct {
	MinSize      int64
	QuietSeconds float64
	DryRun       bool
	Now          time.Time
	Log          func(string)
}

func Deduplicate(alphaRoot, betaRoot string, strategy Strategy, state State, ignores config.IgnoreList,
	opts Options) (Stats, State, error) {
	if opts.Now.IsZero() {
		opts.Now = time.Now()
	}
	if opts.Log == nil {
		opts.Log = func(string) {}
	}
	var stats Stats
	newState := State{}
	now := float64(opts.Now.UnixNano()) / 1e9
	err := candidates(betaRoot, ignores, func(rel string) {
		alpha, beta := filepath.Join(alphaRoot, rel), filepath.Join(betaRoot, rel)
		fail := func(err error) {
			stats.Errors++
			opts.Log(fmt.Sprintf("出错：%s：%v", rel, err))
		}
		betaSt, err := Lstat(beta)
		if err != nil {
			fail(err)
			return
		}
		if betaSt.Size < opts.MinSize {
			stats.Small++
			return
		}
		stats.Examined++
		alphaSt, err := Lstat(alpha)
		if errors.Is(err, fs.ErrNotExist) {
			stats.Missing++
			return
		}
		if err != nil {
			fail(err)
			return
		}
		if !alphaSt.Regular || !betaSt.Regular {
			stats.Missing++
			return
		}
		if alphaSt.Size != betaSt.Size {
			stats.SizeDiffers++
			return
		}
		key := stateKey(alphaSt, betaSt)
		if previous, ok := state[rel]; ok && equal(previous, key) {
			stats.Already++
			stats.AlreadyBytes += betaSt.Size
			newState[rel] = previous
			return
		}
		newest := math.Max(math.Max(float64(alphaSt.Mtime), float64(alphaSt.Ctime)),
			math.Max(float64(betaSt.Mtime), float64(betaSt.Ctime))) / 1e9
		if now-newest < opts.QuietSeconds {
			stats.Recent++
			return
		}
		if opts.DryRun {
			same, err := mutagen.SameContent(alpha, beta)
			if err != nil {
				fail(err)
			} else if same {
				stats.WouldShare++
				stats.WouldShareBytes += betaSt.Size
			} else {
				stats.Differs++
			}
			return
		}
		result, err := strategy.Apply(alpha, beta, alphaSt, betaSt)
		if err != nil {
			fail(err)
			return
		}
		switch result {
		case "shared":
			stats.Shared++
			stats.SharedBytes += betaSt.Size
			a, errA := Lstat(alpha)
			b, errB := Lstat(beta)
			if errA == nil && errB == nil {
				newState[rel] = stateKey(a, b)
			}
		case "differs":
			stats.Differs++
		case "changed":
			stats.Changed++
		}
	})
	return stats, newState, err
}

func equal(a, b []int64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// LoadState 读取状态；文件缺失或损坏时返回空（所有文件重新比较）。
func LoadState(path string) State {
	data, err := os.ReadFile(path)
	if err != nil {
		return State{}
	}
	var state State
	if json.Unmarshal(data, &state) != nil || state == nil {
		return State{}
	}
	return state
}

func SaveState(path string, state State) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, data, 0o644); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

func freeBytes(path string) int64 {
	var st unix.Statfs_t
	if unix.Statfs(path, &st) != nil {
		return 0
	}
	return int64(st.Bavail) * int64(st.Bsize)
}

type RunOptions struct {
	DryRun     bool
	MinSize    int64
	QuietHours float64
	Alpha      string
	Beta       string
	Strategy   Strategy
	Log        func(string)
}

// Run 去重一次并返回统计。做不了时返回 *Unsupported。
func Run(cfg config.Config, opts RunOptions) (Stats, error) {
	alpha, beta := opts.Alpha, opts.Beta
	if alpha == "" {
		alpha = cfg.Workspace
	}
	if beta == "" {
		beta = cfg.LinuxWorkspace()
	}
	for _, path := range []string{alpha, beta} {
		if !util.IsDir(path) {
			return Stats{}, &Unsupported{path + " 不存在"}
		}
	}
	tmpdir := filepath.Join(cfg.TmpDir(), "dedupe")
	if err := os.MkdirAll(tmpdir, 0o755); err != nil {
		return Stats{}, err
	}
	devices := map[uint64]bool{}
	for _, path := range []string{alpha, beta, tmpdir} {
		var st unix.Stat_t
		if err := unix.Stat(path, &st); err != nil {
			return Stats{}, err
		}
		devices[uint64(st.Dev)] = true
	}
	if len(devices) != 1 {
		return Stats{}, &Unsupported{"两棵目录树和运行目录必须在同一个文件系统上"}
	}
	strategy := opts.Strategy
	if strategy == nil {
		var err error
		if strategy, err = DefaultStrategy(tmpdir); err != nil {
			return Stats{}, err
		}
	}
	if err := strategy.Probe(); err != nil {
		return Stats{}, err
	}
	statePath := filepath.Join(cfg.StateDir(), "dedupe.json")
	freeBefore := freeBytes(beta)
	started := time.Now()
	stats, newState, err := Deduplicate(alpha, beta, strategy, LoadState(statePath), cfg.Ignores(), Options{
		MinSize: opts.MinSize, QuietSeconds: opts.QuietHours * 3600, DryRun: opts.DryRun, Log: opts.Log})
	if err != nil {
		return stats, err
	}
	if !opts.DryRun {
		if err := SaveState(statePath, newState); err != nil {
			return stats, err
		}
	}
	stats.Method = strategy.Name()
	stats.DryRun = opts.DryRun
	stats.Seconds = math.Round(time.Since(started).Seconds()*10) / 10
	stats.FreeGainedBytes = freeBytes(beta) - freeBefore
	return stats, nil
}
