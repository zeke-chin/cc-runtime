// Package mutagen 管理把 Mac 工作区镜像到 Linux 工作区的 Mutagen 会话。
package mutagen

import (
	"bytes"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/util"
)

// 文件变化时扫描、暂存、协调都属正常；这些不是。
var unhealthyWords = []string{"Paused", "Halted", "Connecting", "Disconnected", "rescan"}

func CreateCommand(cfg config.Config, alpha, beta, name, binary string) []string {
	if alpha == "" {
		alpha = cfg.Workspace
	}
	if beta == "" {
		beta = cfg.LinuxWorkspace()
	}
	if name == "" {
		name = config.Session
	}
	if binary == "" {
		binary = "mutagen"
	}
	command := []string{binary, "sync", "create", "--name=" + name,
		"--symlink-mode=posix-raw",
		"--default-file-mode=0644", "--default-directory-mode=0755",
		"--ignore-vcs"}
	for _, entry := range cfg.Ignores().Entries {
		command = append(command, "--ignore="+entry)
	}
	return append(command, alpha, beta)
}

type SessionState struct {
	Exists        bool
	DaemonRunning bool
	Status        string
	Connected     bool
	Conflicts     int
	Problems      int
	Ignores       []string
	Raw           string
}

func (s SessionState) Healthy() bool {
	if !s.Exists || !s.Connected || s.Status == "" {
		return false
	}
	for _, word := range unhealthyWords {
		if strings.Contains(s.Status, word) {
			return false
		}
	}
	return true
}

func (s SessionState) OK() bool { return s.Healthy() && s.Conflicts == 0 && s.Problems == 0 }

func ParseSession(raw string) SessionState {
	lines := strings.Split(raw, "\n")
	state := SessionState{Exists: true, DaemonRunning: true, Raw: raw}
	collecting := false
	for _, line := range lines {
		if strings.HasPrefix(line, "\t(alpha) ") {
			state.Conflicts++
		}
		if strings.HasSuffix(strings.ToLower(strings.TrimSpace(line)), "problems:") {
			state.Problems++
		}
		if state.Status == "" && strings.HasPrefix(line, "Status:") {
			state.Status = strings.TrimSpace(strings.TrimPrefix(line, "Status:"))
		}
		if strings.TrimSpace(line) == "Ignores:" {
			collecting = true
			continue
		}
		if collecting {
			if strings.HasPrefix(line, "\t\t") {
				state.Ignores = append(state.Ignores, strings.TrimSpace(line))
			} else {
				collecting = false
			}
		}
	}
	state.Connected = strings.Count(raw, "Connected: Yes") == 2
	return state
}

// Query 读取会话状态。查询时不让 Mutagen 自动启动守护进程：守护进程停了必须能看出来。
func Query(name string) (SessionState, error) {
	binary, err := util.Require("mutagen")
	if err != nil {
		return SessionState{}, err
	}
	result, err := util.Run([]string{binary, "sync", "list", "--long", name},
		util.Opts{Capture: true, AllowFail: true, Env: []string{"MUTAGEN_DISABLE_AUTOSTART=1"}})
	if err != nil {
		return SessionState{}, err
	}
	raw := result.Stdout + result.Stderr
	if result.Code != 0 {
		return SessionState{Exists: false, DaemonRunning: !strings.Contains(strings.ToLower(raw), "daemon"), Raw: raw}, nil
	}
	return ParseSession(raw), nil
}

// IgnoreDrift 返回（只在配置里的条目，只在运行中会话里的条目）。
func IgnoreDrift(cfg config.Config, state SessionState) (missing, extra []string) {
	wanted := map[string]bool{}
	for _, entry := range cfg.Ignores().Entries {
		wanted[entry] = true
	}
	actual := map[string]bool{}
	for _, entry := range state.Ignores {
		actual[entry] = true
	}
	for entry := range wanted {
		if !actual[entry] {
			missing = append(missing, entry)
		}
	}
	for entry := range actual {
		if !wanted[entry] {
			extra = append(extra, entry)
		}
	}
	sort.Strings(missing)
	sort.Strings(extra)
	return missing, extra
}

func Create(cfg config.Config) error {
	state, err := Query(config.Session)
	if err != nil {
		return err
	}
	if state.Exists {
		return util.Errorf("会话 %q 已经存在", config.Session)
	}
	if !util.Lexists(filepath.Join(cfg.Root, config.ProjectIgnoresFile)) {
		return util.Errorf("缺少 %s；没有它依赖目录会被同步", filepath.Join(cfg.Root, config.ProjectIgnoresFile))
	}
	for _, path := range []string{cfg.Workspace, cfg.LinuxWorkspace(), cfg.GitStore()} {
		if !util.IsDir(path) {
			return util.Errorf("%s 不存在", path)
		}
	}
	binary, err := util.Require("mutagen")
	if err != nil {
		return err
	}
	_, err = util.Run(CreateCommand(cfg, "", "", "", binary), util.Opts{})
	return err
}

func Flush() {
	if binary := util.Which("mutagen"); binary != "" {
		util.Run([]string{binary, "sync", "flush", config.Session}, util.Opts{Capture: true, AllowFail: true})
	}
}

type entry struct {
	kind       string // file、dir、link
	size       int64
	target     string
	executable bool
}

func (e entry) String() string {
	switch e.kind {
	case "link":
		return "链接 -> " + e.target
	case "dir":
		return "目录"
	}
	mode := ""
	if e.executable {
		mode = "，可执行"
	}
	return fmt.Sprintf("文件 %d 字节%s", e.size, mode)
}

// snapshot 返回忽略名单之外的条目。真实的 .git 目录跳过（从不同步）；.git 链接参与比较。
func snapshot(root string, ignores config.IgnoreList) (map[string]entry, error) {
	entries := map[string]entry{}
	err := util.Walk(root, func(rel string, d fs.DirEntry) error {
		if rel == "" {
			return nil
		}
		if ignores.Matches(rel) {
			if d.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		path := filepath.Join(root, filepath.FromSlash(rel))
		switch {
		case d.Type()&fs.ModeSymlink != 0:
			target, _ := os.Readlink(path)
			entries[rel] = entry{kind: "link", target: target}
		case d.IsDir():
			if d.Name() == ".git" {
				return fs.SkipDir
			}
			entries[rel] = entry{kind: "dir"}
		default:
			info, err := d.Info()
			if err != nil {
				return nil
			}
			entries[rel] = entry{kind: "file", size: info.Size(), executable: info.Mode()&0o100 != 0}
		}
		return nil
	})
	return entries, err
}

// Compare 列出两棵树在忽略名单之外的差异，供人阅读。
func Compare(alpha, beta string, ignores config.IgnoreList, content bool) ([]string, error) {
	a, err := snapshot(alpha, ignores)
	if err != nil {
		return nil, err
	}
	b, err := snapshot(beta, ignores)
	if err != nil {
		return nil, err
	}
	var differences []string
	for _, rel := range sortedKeys(a) {
		if _, ok := b[rel]; !ok {
			differences = append(differences, fmt.Sprintf("仅在 %s：%s", alpha, rel))
		}
	}
	for _, rel := range sortedKeys(b) {
		if _, ok := a[rel]; !ok {
			differences = append(differences, fmt.Sprintf("仅在 %s：%s", beta, rel))
		}
	}
	for _, rel := range sortedKeys(a) {
		other, ok := b[rel]
		if !ok {
			continue
		}
		if a[rel] != other {
			differences = append(differences, fmt.Sprintf("不同：%s：%s != %s", rel, a[rel], other))
		} else if content && a[rel].kind == "file" {
			same, err := SameContent(filepath.Join(alpha, rel), filepath.Join(beta, rel))
			if err != nil || !same {
				differences = append(differences, "内容不同："+rel)
			}
		}
	}
	return differences, nil
}

func sortedKeys(m map[string]entry) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// SameContent 逐字节比较两个文件。
func SameContent(a, b string) (bool, error) {
	fa, err := os.Open(a)
	if err != nil {
		return false, err
	}
	defer fa.Close()
	fb, err := os.Open(b)
	if err != nil {
		return false, err
	}
	defer fb.Close()
	ia, err := fa.Stat()
	if err != nil {
		return false, err
	}
	ib, err := fb.Stat()
	if err != nil {
		return false, err
	}
	if !ia.Mode().IsRegular() || !ib.Mode().IsRegular() || ia.Size() != ib.Size() {
		return false, nil
	}
	bufA, bufB := make([]byte, 1<<20), make([]byte, 1<<20)
	for {
		na, errA := io.ReadFull(fa, bufA)
		nb, errB := io.ReadFull(fb, bufB)
		if na != nb || !bytes.Equal(bufA[:na], bufB[:nb]) {
			return false, nil
		}
		if errA == io.EOF || errA == io.ErrUnexpectedEOF {
			return errB == io.EOF || errB == io.ErrUnexpectedEOF, nil
		}
		if errA != nil {
			return false, errA
		}
		if errB != nil {
			return false, errB
		}
	}
}
