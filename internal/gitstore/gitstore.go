// Package gitstore 把每个仓库的 git 数据放到两棵目录树之外的同一个共享目录。
//
// Mutagen 不同步真实的 .git 目录。迁出一个仓库时，把它的 .git 目录改名为
// <共享目录>/<相对路径>.git，原处留一个绝对路径的符号链接；Mutagen 把这个链接
// 复制到另一棵树，两侧就操作同一份 git 数据，由 git 自己的锁协调。
//
// 开发机把 Linux 树挂在和 Mac 工作区相同的路径上，共享目录也一样，所以 git 记下
// 的路径（worktree 位置、子模块的 core.worktree）在两侧指向同一个地方。迁出和
// 恢复只需把随 git 目录移动而失效的路径改写一次，见 relink。
package gitstore

import (
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/util"
)

// QuietSeconds：git 数据在这么多秒内改动过的仓库先跳过。
const QuietSeconds = 120

const AlreadyExternalized = "已经迁出"

// LegacyDevWorkspace 是开发机以前挂载 Linux 树的位置。那时在开发机上建的
// worktree 和子模块记下的是这个路径，relink 把它换成工作区路径。
const LegacyDevWorkspace = "/home/dev/workspace"

type Layout struct {
	Mac, Linux, Store string
	Ignores           config.IgnoreList
	// Aliases 是其他表示工作区的路径（旧的开发机挂载点），relink 会改写它们。
	Aliases []string
}

func FromConfig(cfg config.Config) Layout {
	return Layout{Mac: cfg.Workspace, Linux: cfg.LinuxWorkspace(), Store: cfg.GitStore(), Ignores: cfg.Ignores(),
		Aliases: []string{LegacyDevWorkspace}}
}

// canonical 把任一棵树或别名下的路径换成工作区路径（两侧共用的那个）；
// 不在工作区内时返回 false。
func (l Layout) canonical(path string) (string, bool) {
	path = filepath.Clean(path)
	for _, root := range append([]string{l.Mac, l.Linux}, l.Aliases...) {
		root = filepath.Clean(root)
		if path == root {
			return filepath.Clean(l.Mac), true
		}
		if within(path, root) {
			return filepath.Join(l.Mac, strings.TrimPrefix(path, root+"/")), true
		}
	}
	return "", false
}

// physical 是工作区路径在 Mac 上能访问到的两个位置（Mac 树和 Linux 树）。
func (l Layout) physical(canonical string) []string {
	rel, err := filepath.Rel(l.Mac, canonical)
	if err != nil || rel == ".." || strings.HasPrefix(rel, "../") {
		return []string{canonical}
	}
	return []string{filepath.Join(l.Mac, rel), filepath.Join(l.Linux, rel)}
}

type Tree struct{ Side, Root string }

func (l Layout) Trees() []Tree { return []Tree{{"mac", l.Mac}, {"linux", l.Linux}} }

func (l Layout) root(side string) string {
	if side == "mac" {
		return l.Mac
	}
	return l.Linux
}

func (l Layout) Target(rel string) string { return filepath.Join(l.Store, rel+".git") }

// within 判断 path 是否在 dir 之内（不含 dir 本身），只比较路径文字。
func within(path, dir string) bool {
	return strings.HasPrefix(filepath.Clean(path), strings.TrimRight(filepath.Clean(dir), "/")+"/")
}

// Rel 把命令行参数（相对工作区的路径，或任一棵树里的绝对路径）转成相对路径。
func (l Layout) Rel(argument string) (string, error) {
	var rel string
	if !filepath.IsAbs(argument) {
		rel = strings.Trim(filepath.ToSlash(argument), "/")
	} else {
		found := false
		for _, tree := range l.Trees() {
			if r, err := filepath.Rel(tree.Root, argument); err == nil && r != ".." && !strings.HasPrefix(r, "../") {
				rel, found = filepath.ToSlash(r), true
				break
			}
		}
		if !found {
			return "", fmt.Errorf("%s 不在 %s 或 %s 之内", argument, l.Mac, l.Linux)
		}
	}
	for _, part := range strings.Split(rel, "/") {
		if part == ".." {
			return "", fmt.Errorf("无效的仓库路径：%s", argument)
		}
	}
	rel = filepath.ToSlash(filepath.Clean(rel))
	if rel == "" || rel == "." {
		return "", fmt.Errorf("无效的仓库路径：%s", argument)
	}
	return rel, nil
}

func (l Layout) Check() error {
	if !util.IsDir(l.Mac) {
		return fmt.Errorf("%s 不存在", l.Mac)
	}
	store := filepath.Clean(l.Store)
	for _, root := range []string{l.Mac, l.Linux} {
		root = filepath.Clean(root)
		if root == store || within(store, root) || within(root, store) {
			return fmt.Errorf("git 共享目录必须在 %s 之外", root)
		}
	}
	return nil
}

// Kind 返回 "link"、"dir"、"file"，不存在时返回 ""。
func Kind(path string) string {
	info, err := os.Lstat(path)
	if err != nil {
		return ""
	}
	if info.Mode()&fs.ModeSymlink != 0 {
		return "link"
	}
	if info.IsDir() {
		return "dir"
	}
	return "file"
}

// ScanTree 返回 仓库相对路径 -> 其 .git 条目的类型。
func ScanTree(root string, ignores config.IgnoreList) map[string]string {
	found := map[string]string{}
	if !util.IsDir(root) {
		return found
	}
	util.Walk(root, func(rel string, d fs.DirEntry) error {
		if d.Name() == ".git" && rel != "" {
			if d.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if !d.IsDir() {
			return nil
		}
		if rel != "" && ignores.Matches(rel) {
			return fs.SkipDir
		}
		if rel != "" { // 工作区根目录本身不算仓库
			if kind := Kind(filepath.Join(root, filepath.FromSlash(rel), ".git")); kind != "" {
				found[rel] = kind
			}
		}
		return nil
	})
	return found
}

type Entries struct{ Mac, Linux string }

func (e Entries) Get(side string) string {
	if side == "mac" {
		return e.Mac
	}
	return e.Linux
}

func (e Entries) Has(kind string) bool { return e.Mac == kind || e.Linux == kind }

func Scan(l Layout) map[string]Entries {
	repos := map[string]Entries{}
	for rel, kind := range ScanTree(l.Mac, l.Ignores) {
		e := repos[rel]
		e.Mac = kind
		repos[rel] = e
	}
	for rel, kind := range ScanTree(l.Linux, l.Ignores) {
		e := repos[rel]
		e.Linux = kind
		repos[rel] = e
	}
	return repos
}

func SortedRels(repos map[string]Entries) []string {
	rels := make([]string, 0, len(repos))
	for rel := range repos {
		rels = append(rels, rel)
	}
	sort.Strings(rels)
	return rels
}

// moduleGitdirs 列出 gitdir 下所有子模块的 git 目录（含嵌套的子模块）。
func moduleGitdirs(gitdir string) []string {
	var found []string
	var visit func(dir string)
	visit = func(dir string) {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return
		}
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			path := filepath.Join(dir, e.Name())
			if util.Lexists(filepath.Join(path, "HEAD")) && util.Lexists(filepath.Join(path, "config")) {
				found = append(found, path)
				visit(filepath.Join(path, "modules"))
			} else {
				visit(path) // 子模块名可以含 /
			}
		}
	}
	visit(filepath.Join(gitdir, "modules"))
	return found
}

// worktreeMetas 列出 gitdir 下每个关联 worktree 的元数据目录。
func worktreeMetas(gitdir string) []string {
	var found []string
	entries, _ := os.ReadDir(filepath.Join(gitdir, "worktrees"))
	for _, e := range entries {
		if e.IsDir() {
			found = append(found, filepath.Join(gitdir, "worktrees", e.Name()))
		}
	}
	return found
}

// resolveFrom 把 git 记下的路径解析成绝对路径；相对路径相对 base（只做文字运算）。
func resolveFrom(base, recorded string) string {
	if filepath.IsAbs(recorded) {
		return filepath.Clean(recorded)
	}
	return filepath.Join(base, recorded)
}

func writeAtomic(path, content string) error {
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	temporary := path + ".cc-runtime.tmp"
	if err := os.WriteFile(temporary, []byte(content), info.Mode().Perm()); err != nil {
		return err
	}
	if err := os.Rename(temporary, path); err != nil {
		os.Remove(temporary)
		return err
	}
	return nil
}

// fixGitfile 让检出目录 dir 里的 .git 文件指向 meta。dir 是工作区路径，两侧的
// 副本都检查；记下的路径从 dir 出发已经能到达 meta 时不改（保留 git 写的相对路径）。
func (l Layout) fixGitfile(dir, meta string) ([]string, error) {
	var changed []string
	want, err := os.Stat(meta)
	if err != nil {
		return nil, nil
	}
	for _, copy := range l.physical(dir) {
		path := filepath.Join(copy, ".git")
		if Kind(path) != "file" {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return changed, err
		}
		recorded, ok := strings.CutPrefix(strings.TrimSpace(string(data)), "gitdir:")
		if !ok {
			continue
		}
		// 按工作区路径解析：两侧路径相同，Mac 上解析的结果就是开发机上的结果。
		if got, err := os.Stat(resolveFrom(dir, strings.TrimSpace(recorded))); err == nil && os.SameFile(got, want) {
			continue
		}
		if err := writeAtomic(path, "gitdir: "+meta+"\n"); err != nil {
			return changed, err
		}
		changed = append(changed, path)
	}
	return changed, nil
}

// relink 改写随 git 目录移动而失效、或只在一侧有效的路径。oldGitdir 是移动前的
// 位置（解析相对路径用），newGitdir 是移动后的位置（两侧共用，也是 Mac 上的实际
// 位置）；两者相同时只修正别名和只在一侧有效的路径。返回改写过的条目。
func (l Layout) relink(oldGitdir, newGitdir string) ([]string, error) {
	var changed []string
	add := func(items []string, err error) error {
		changed = append(changed, items...)
		return err
	}
	for _, meta := range worktreeMetas(newGitdir) {
		file := filepath.Join(meta, "gitdir")
		data, err := os.ReadFile(file)
		if err != nil {
			continue
		}
		old := strings.TrimSpace(string(data))
		location := resolveFrom(filepath.Join(oldGitdir, "worktrees", filepath.Base(meta)), old)
		if canonical, ok := l.canonical(location); ok {
			location = canonical
		}
		if location != old {
			if err := writeAtomic(file, location+"\n"); err != nil {
				return changed, err
			}
			changed = append(changed, file)
		}
		if err := add(l.fixGitfile(filepath.Dir(location), meta)); err != nil {
			return changed, err
		}
	}
	for _, module := range moduleGitdirs(newGitdir) {
		config := filepath.Join(module, "config")
		out, err := exec.Command("git", "config", "-f", config, "--get", "core.worktree").Output()
		old := strings.TrimSpace(string(out))
		if err != nil || old == "" {
			continue
		}
		suffix, _ := filepath.Rel(newGitdir, module)
		location, ok := l.canonical(resolveFrom(filepath.Join(oldGitdir, suffix), old))
		if !ok {
			continue
		}
		// git 自己写相对路径；两侧路径相同，这个相对路径在两侧都成立。
		value, err := filepath.Rel(module, location)
		if err != nil {
			return changed, err
		}
		if value != old {
			if out, err := exec.Command("git", "config", "-f", config, "core.worktree", value).CombinedOutput(); err != nil {
				return changed, fmt.Errorf("%s：%s", config, strings.TrimSpace(string(out)))
			}
			changed = append(changed, config)
		}
		if err := add(l.fixGitfile(location, module)); err != nil {
			return changed, err
		}
	}
	return changed, nil
}

// OutsideWorktrees 列出 gitdir 关联的、不在工作区内的 worktree：另一侧没有
// 这个路径，只有创建它的一侧能用。
func (l Layout) OutsideWorktrees(gitdir string) []string {
	var found []string
	for _, meta := range worktreeMetas(gitdir) {
		data, err := os.ReadFile(filepath.Join(meta, "gitdir"))
		if err != nil {
			continue
		}
		location := resolveFrom(meta, strings.TrimSpace(string(data)))
		if _, ok := l.canonical(location); !ok {
			found = append(found, filepath.Dir(location))
		}
	}
	return found
}

// lockIn 返回 dir 里第一个 git 锁文件的名字。
func lockIn(dir string) string {
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".lock") && e.Type().IsRegular() {
			return e.Name()
		}
	}
	return ""
}

func mtime(path string) (time.Time, bool) {
	info, err := os.Stat(path)
	if err != nil {
		return time.Time{}, false
	}
	return info.ModTime(), true
}

// Busy 说明 git 数据为什么现在可能正被使用；空闲时返回 ""。
func Busy(gitdir string, now time.Time) string {
	// 只看 git 自己加锁的地方：工具会在 .git 里放长期存在的文件
	// （Cursor 会留下 .git/cursor/crepe/build.lock）。worktree 和子模块各有自己的锁。
	for _, dir := range append(append([]string{gitdir}, worktreeMetas(gitdir)...), moduleGitdirs(gitdir)...) {
		if name := lockIn(dir); name != "" {
			rel, _ := filepath.Rel(gitdir, filepath.Join(dir, name))
			return "有锁文件 " + filepath.ToSlash(rel)
		}
	}
	lock := ""
	util.Walk(filepath.Join(gitdir, "refs"), func(rel string, d fs.DirEntry) error {
		if !d.IsDir() && strings.HasSuffix(d.Name(), ".lock") {
			lock = "refs/" + rel
			return fs.SkipAll
		}
		return nil
	})
	if lock != "" {
		return "有锁文件 " + lock
	}
	objects := filepath.Join(gitdir, "objects")
	if names, err := os.ReadDir(objects); err == nil {
		for _, e := range names {
			if strings.HasPrefix(e.Name(), "incoming-") {
				return "正在接收对象"
			}
		}
		if packs, err := os.ReadDir(filepath.Join(objects, "pack")); err == nil {
			for _, e := range packs {
				if strings.HasPrefix(e.Name(), "tmp_pack_") || strings.HasPrefix(e.Name(), "tmp_idx_") {
					return "正在传输 pack"
				}
			}
		}
	}
	var newest time.Time
	for _, marker := range []string{"", "index", "HEAD", "packed-refs", "ORIG_HEAD", "logs/HEAD",
		"objects", "objects/pack", "refs/heads"} {
		if t, ok := mtime(filepath.Join(gitdir, marker)); ok && t.After(newest) {
			newest = t
		}
	}
	if age := now.Sub(newest); age < QuietSeconds*time.Second {
		return fmt.Sprintf("git 数据 %d 秒前有改动", int(age.Seconds()))
	}
	if exec.Command("git", "--git-dir="+gitdir, "rev-parse", "-q", "--verify", "HEAD").Run() != nil {
		return "还没有提交（可能仍在克隆）"
	}
	return ""
}

func Externalize(l Layout, rel string, dryRun bool) (bool, string, error) {
	target := l.Target(rel)
	entries := Entries{Mac: Kind(filepath.Join(l.Mac, rel, ".git")), Linux: Kind(filepath.Join(l.Linux, rel, ".git"))}
	var real []string
	for _, tree := range l.Trees() {
		if entries.Get(tree.Side) == "dir" {
			real = append(real, tree.Side)
		}
	}
	if len(real) == 0 {
		if entries.Has("link") {
			if !dryRun {
				if changed, message, err := Repair(l, rel); changed || err != nil {
					return changed, message, err
				}
			}
			return false, rel + "：" + AlreadyExternalized, nil
		}
		return false, rel + "：没有 .git 目录", nil
	}
	if len(real) == 2 {
		return false, rel + "：拒绝，两棵目录树各有自己的 .git 目录", nil
	}
	side := real[0]
	other := "mac"
	if side == "mac" {
		other = "linux"
	}
	if entries.Get(other) != "" {
		return false, fmt.Sprintf("%s：拒绝，%s 目录树里有 .git（%s）", rel, other, entries.Get(other)), nil
	}
	root := l.root(side)
	gitdir := filepath.Join(root, rel, ".git")
	out, _ := exec.Command("git", "config", "-f", filepath.Join(gitdir, "config"), "core.worktree").Output()
	if strings.TrimSpace(string(out)) != "" {
		return false, rel + "：跳过，设置了 core.worktree", nil
	}
	if reason := Busy(gitdir, time.Now()); reason != "" {
		return false, rel + "：跳过，" + reason, nil
	}
	if util.Lexists(target) {
		return false, fmt.Sprintf("%s：拒绝，%s 已经存在", rel, target), nil
	}
	if dryRun {
		return true, fmt.Sprintf("%s：将移动 %s -> %s", rel, gitdir, target), nil
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return false, "", err
	}
	if err := os.Rename(gitdir, target); err != nil {
		return false, "", err
	}
	if err := os.Symlink(target, gitdir); err != nil {
		os.Rename(target, gitdir)
		return false, "", err
	}
	message := fmt.Sprintf("%s：已从 %s 目录树迁出", rel, side)
	changed, err := l.relink(gitdir, target)
	if len(changed) > 0 {
		message += fmt.Sprintf("，改写了 %d 处 worktree/子模块路径", len(changed))
	}
	return true, message, err
}

// Repair 修正已迁出仓库里只在一侧有效的 worktree 和子模块路径（旧挂载点、
// Linux 树在 Mac 上的路径）。没有可修正的或仓库正忙时什么也不做。
func Repair(l Layout, rel string) (bool, string, error) {
	target := l.Target(rel)
	if !util.IsDir(target) || Busy(target, time.Now()) != "" {
		return false, "", nil
	}
	changed, err := l.relink(target, target)
	if len(changed) == 0 {
		return false, "", err
	}
	return true, fmt.Sprintf("%s：改写了 %d 处 worktree/子模块路径", rel, len(changed)), err
}

func Restore(l Layout, rel string, dryRun bool) (bool, string, error) {
	link := filepath.Join(l.Mac, rel, ".git")
	if Kind(link) != "link" {
		return false, rel + "：在 mac 目录树中未迁出", nil
	}
	target, err := os.Readlink(link)
	if err != nil {
		return false, "", err
	}
	target = filepath.Clean(target)
	if !filepath.IsAbs(target) || !within(target, l.Store) || !util.IsDir(target) {
		return false, fmt.Sprintf("%s：拒绝，链接目标 %s 不是 %s 里的目录", rel, target, l.Store), nil
	}
	if reason := Busy(target, time.Now()); reason != "" {
		return false, rel + "：跳过，" + reason, nil
	}
	if dryRun {
		return true, fmt.Sprintf("%s：将移动 %s -> %s", rel, target, link), nil
	}
	other := filepath.Join(l.Linux, rel, ".git")
	if err := os.Remove(link); err != nil {
		return false, "", err
	}
	if err := os.Rename(target, link); err != nil {
		os.Symlink(target, link)
		return false, "", err
	}
	message := rel + "：已恢复"
	if changed, err := l.relink(target, link); err != nil {
		return true, message, err
	} else if len(changed) > 0 {
		message += fmt.Sprintf("，改写了 %d 处 worktree/子模块路径", len(changed))
	}
	// 同步也会删掉它；在这里删不依赖会话。
	if Kind(other) == "link" {
		if t, err := os.Readlink(other); err == nil && filepath.Clean(t) == target {
			os.Remove(other)
		}
	}
	store := filepath.Clean(l.Store)
	for parent := filepath.Dir(target); parent != store && within(parent, store); parent = filepath.Dir(parent) {
		if entries, err := os.ReadDir(parent); err != nil || len(entries) > 0 || os.Remove(parent) != nil {
			break
		}
	}
	return true, message, nil
}

// StoredGitdirs 列出共享目录里的所有 git 目录。
func StoredGitdirs(store string) map[string]bool {
	found := map[string]bool{}
	if !util.IsDir(store) {
		return found
	}
	util.Walk(store, func(rel string, d fs.DirEntry) error {
		if rel != "" && d.IsDir() && strings.HasSuffix(rel, ".git") {
			found[filepath.Join(store, filepath.FromSlash(rel))] = true
			return fs.SkipDir
		}
		return nil
	})
	return found
}

type Report struct {
	Counts       map[string]int
	Externalized []string
	Warnings     []string
	Anomalies    []string
}

func (r *Report) add(key string) { r.Counts[key]++ }

func (r *Report) SortedCounts() []string {
	keys := make([]string, 0, len(r.Counts))
	for key := range r.Counts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func Status(l Layout) *Report {
	report := &Report{Counts: map[string]int{}}
	repos := Scan(l)
	users := map[string]map[string]bool{}
	for _, rel := range SortedRels(repos) {
		e := repos[rel]
		if e.Has("file") && !e.Has("link") && !e.Has("dir") {
			report.add("gitfile（子模块或 worktree 检出）")
			continue
		}
		if e.Has("link") {
			targets := map[string]bool{}
			for _, tree := range l.Trees() {
				if e.Get(tree.Side) == "link" {
					if t, err := os.Readlink(filepath.Join(tree.Root, rel, ".git")); err == nil {
						targets[filepath.Clean(t)] = true
					}
				}
			}
			if len(targets) > 1 {
				report.Anomalies = append(report.Anomalies, rel+"：两棵目录树链接到不同的 git 目录")
			}
			if e.Has("dir") {
				report.Anomalies = append(report.Anomalies, rel+"：一侧是链接，另一侧是真实的 .git 目录")
			} else if e.Mac == "" || e.Linux == "" {
				missing := "mac"
				if e.Linux == "" {
					missing = "linux"
				}
				report.Warnings = append(report.Warnings, fmt.Sprintf("%s：只有一侧有链接；%s 目录树还没同步到", rel, missing))
			}
			for _, target := range sortedSet(targets) {
				if users[target] == nil {
					users[target] = map[string]bool{}
				}
				users[target][rel] = true
				if !util.IsDir(target) {
					report.Anomalies = append(report.Anomalies, fmt.Sprintf("%s：链接目标 %s 不存在", rel, target))
					continue
				}
				if target != filepath.Clean(l.Target(rel)) {
					report.Anomalies = append(report.Anomalies, fmt.Sprintf(
						"%s：链接目标 %s 与路径不符；应为 %s（被移动或复制过？）", rel, target, l.Target(rel)))
				}
				for _, outside := range l.OutsideWorktrees(target) {
					report.Warnings = append(report.Warnings, fmt.Sprintf(
						"%s：worktree %s 在工作区之外，只有创建它的一侧能用", rel, outside))
				}
			}
			report.add("已迁出")
			report.Externalized = append(report.Externalized, rel)
			continue
		}
		side := "linux"
		if e.Mac == "dir" && e.Linux == "dir" {
			side = "both"
		} else if e.Mac == "dir" {
			side = "mac"
		}
		report.add(fmt.Sprintf("未迁出（%s 目录树）", side))
		if side == "both" {
			report.Warnings = append(report.Warnings, rel+"：两棵目录树各有自己的 .git 目录")
		}
	}
	for _, target := range sortedKeys(users) {
		if rels := users[target]; len(rels) > 1 {
			report.Anomalies = append(report.Anomalies, fmt.Sprintf("%s：被多个工作树共用：%s（用 cp -R 复制过？）",
				target, strings.Join(sortedSet(rels), ", ")))
		}
	}
	stored := StoredGitdirs(l.Store)
	for _, orphan := range sortedSet(stored) {
		if users[orphan] == nil {
			report.Anomalies = append(report.Anomalies, orphan+"：没有工作树链接到它（项目已删除？）")
		}
	}
	return report
}

type Result struct {
	Rel     string
	Changed bool
	Message string
	Repair  bool // 已迁出仓库的路径修正，不是迁出
}

func ExternalizeAll(l Layout, dryRun bool) []Result {
	var results []Result
	repos := Scan(l)
	for _, rel := range SortedRels(repos) {
		e := repos[rel]
		var changed, repair bool
		var message string
		var err error
		switch {
		case e.Has("dir"):
			changed, message, err = Externalize(l, rel, dryRun)
		case e.Has("link") && !dryRun:
			repair = true
			if changed, message, err = Repair(l, rel); !changed && err == nil {
				continue
			}
		default:
			continue
		}
		if err != nil {
			message = fmt.Sprintf("%s：出错：%v", rel, err)
		}
		results = append(results, Result{rel, changed, message, repair})
	}
	return results
}

func sortedSet(set map[string]bool) []string {
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
