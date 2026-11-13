// Package checks 是一致性检查：同步忽略名单和网关配置。
package checks

import (
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/deploy"
	"github.com/zeke-chin/cc-runtime/internal/gitstore"
	"github.com/zeke-chin/cc-runtime/internal/util"
)

// ---- 同步忽略名单 ---------------------------------------------------------

func contains(a, b string) bool {
	a, b = filepath.Clean(a), filepath.Clean(b)
	return a == b || strings.HasPrefix(b, strings.TrimRight(a, "/")+"/")
}

func LayoutErrors(cfg config.Config) []string {
	var errors []string
	paths := []string{cfg.Workspace, cfg.LinuxWorkspace(), cfg.GitStore()}
	for i, x := range paths {
		for _, y := range paths[i+1:] {
			if contains(x, y) || contains(y, x) {
				errors = append(errors, fmt.Sprintf("%s 和 %s 不能互相包含", x, y))
			}
		}
	}
	if !util.Lexists(filepath.Join(cfg.Root, config.ProjectIgnoresFile)) {
		errors = append(errors, fmt.Sprintf("缺少 %s：依赖目录会被同步", filepath.Join(cfg.Root, config.ProjectIgnoresFile)))
	}
	if rel, ok := cfg.RepoRel(); ok && !cfg.Ignores().Matches(rel+"/dev-runtimes/x") {
		errors = append(errors, "忽略名单必须排除本仓库的 dev-runtimes/")
	}
	return errors
}

type Violation struct{ Repo, Name string }

// TrackedViolations 是被 git 跟踪、却落在忽略名单里的文件（这些源码不会同步）。
func TrackedViolations(workspace string, ignores config.IgnoreList) []Violation {
	var found []Violation
	repos := gitstore.ScanTree(workspace, ignores)
	rels := make([]string, 0, len(repos))
	for rel := range repos {
		rels = append(rels, rel)
	}
	sort.Strings(rels)
	for _, rel := range rels {
		if kind := repos[rel]; kind != "dir" && kind != "link" {
			continue // 子模块检出由上层仓库覆盖
		}
		cmd := exec.Command("git", "-C", filepath.Join(workspace, rel), "-c", "core.quotepath=off", "ls-files", "-z")
		var stderr strings.Builder
		cmd.Stderr = &stderr
		out, err := cmd.Output()
		if err != nil {
			found = append(found, Violation{rel, "git ls-files 失败：" + strings.TrimSpace(stderr.String())})
			continue
		}
		for _, name := range strings.Split(string(out), "\x00") {
			if name != "" && ignores.Matches(rel+"/"+name) {
				found = append(found, Violation{rel, name})
			}
		}
	}
	return found
}

// StrayVirtualenvs 是不在忽略名字里的虚拟环境：它们会被同步。
func StrayVirtualenvs(workspace string, ignores config.IgnoreList) []string {
	var found []string
	util.Walk(workspace, func(rel string, d fs.DirEntry) error {
		if !d.IsDir() {
			return nil
		}
		if rel != "" && (d.Name() == ".git" || ignores.Matches(rel)) {
			return fs.SkipDir
		}
		if info, err := os.Lstat(filepath.Join(workspace, rel, "pyvenv.cfg")); err == nil && !info.IsDir() {
			found = append(found, rel)
			return fs.SkipDir
		}
		return nil
	})
	return found
}

type StateMount struct{ Container, Rel string }

// inspectAll 列出所有容器（可在测试中替换）。
var inspectAll = func() ([]deploy.Container, error) {
	docker := util.Which("docker")
	if docker == "" {
		return nil, nil
	}
	result, err := util.Run([]string{docker, "ps", "-aq"}, util.Opts{Capture: true})
	if err != nil {
		return nil, err
	}
	return deploy.Inspect(strings.Fields(result.Stdout))
}

// ContainerStateMounts 是容器以可写方式挂载的、在工作区里且没被忽略的目录。
//
// 活的状态（比如数据库目录）不能同步：会在写到一半时被复制，Linux 侧的改动也会
// 流回去。挂载整个工作区的不算。
func ContainerStateMounts(workspace string, ignores config.IgnoreList) ([]StateMount, error) {
	containers, err := inspectAll()
	if err != nil {
		return nil, err
	}
	workspace = filepath.Clean(workspace)
	seen := map[StateMount]bool{}
	var found []StateMount
	for _, c := range containers {
		for _, m := range c.Mounts {
			if m.Type != "bind" || !m.RW {
				continue
			}
			source := filepath.Clean(m.Source)
			if source == workspace || !strings.HasPrefix(source, workspace+"/") {
				continue
			}
			rel := strings.TrimPrefix(source, workspace+"/")
			name := c.Name
			if name == "" {
				name = "?"
			}
			item := StateMount{strings.TrimPrefix(name, "/"), rel}
			if !ignores.Matches(rel+"/") && !seen[item] {
				seen[item] = true
				found = append(found, item)
			}
		}
	}
	sort.Slice(found, func(i, j int) bool {
		if found[i].Container != found[j].Container {
			return found[i].Container < found[j].Container
		}
		return found[i].Rel < found[j].Rel
	})
	return found, nil
}

// ---- 网关配置 ----------------------------------------------------------

var (
	block   = regexp.MustCompile(`(?s)[ ]*# ==== GATEWAY-SPECIFIC.*?# ==== END GATEWAY-SPECIFIC ====\n`)
	airport = regexp.MustCompile(`(?s)  - name: airport\n.*?\n\n`)
	// 模板里的占位 IP，后面紧跟本地局域网注释。
	placeholderIPs = regexp.MustCompile(`IP-CIDR,[0-9.]+/32,DIRECT,no-resolve\n(  # Local LAN subnet)`)
	credential     = regexp.MustCompile(`(password|username|uuid|public-key|short-id|cipher)\s*:`)
)

func NormalizeProxy(text string) (string, error) {
	start := strings.Index(text, "mode: rule\n")
	if start < 0 {
		return "", fmt.Errorf("找不到 \"mode: rule\"")
	}
	text = text[start:]
	text = block.ReplaceAllString(text, "<GATEWAY-SPECIFIC>\n")
	text = airport.ReplaceAllString(text, "<AIRPORT>\n\n")
	return placeholderIPs.ReplaceAllString(text, "<DIRECT-HOST>\n$1"), nil
}

type ProxyResult struct {
	Name  string
	Lines []string
}

// ProxyDifferences 对每个 config*.yaml（模板除外）给出它与模板的差异。
//
// 在 GATEWAY-SPECIFIC 块之外，每份实际配置都必须与 config.example.yaml 相同，
// 机场节点和文档里说明的占位符除外。像凭据的行从不显示。
func ProxyDifferences(proxyDir string) ([]ProxyResult, error) {
	template, err := os.ReadFile(filepath.Join(proxyDir, "config.example.yaml"))
	if err != nil {
		return nil, err
	}
	normalized, err := NormalizeProxy(string(template))
	if err != nil {
		return nil, fmt.Errorf("config.example.yaml：%w", err)
	}
	expected := strings.Split(normalized, "\n")
	paths, _ := filepath.Glob(filepath.Join(proxyDir, "config*.yaml"))
	sort.Strings(paths)
	var results []ProxyResult
	for _, path := range paths {
		name := filepath.Base(path)
		if name == "config.example.yaml" {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		text, err := NormalizeProxy(string(data))
		if err != nil {
			return nil, fmt.Errorf("%s：%w", name, err)
		}
		var lines []string
		for _, line := range UnifiedDiff(expected, strings.Split(text, "\n"), "config.example.yaml", name, 1) {
			if credential.MatchString(line) {
				line = line[:1] + "  <已隐藏凭据行>"
			}
			lines = append(lines, line)
		}
		results = append(results, ProxyResult{name, lines})
	}
	return results, nil
}

// UnifiedDiff 生成 unified diff 格式的差异（相同时为空）。
func UnifiedDiff(a, b []string, fromName, toName string, context int) []string {
	// 最长公共子序列；配置文件只有几百行，O(n·m) 足够。
	n, m := len(a), len(b)
	lcs := make([][]int, n+1)
	for i := range lcs {
		lcs[i] = make([]int, m+1)
	}
	for i := n - 1; i >= 0; i-- {
		for j := m - 1; j >= 0; j-- {
			if a[i] == b[j] {
				lcs[i][j] = lcs[i+1][j+1] + 1
			} else {
				lcs[i][j] = max(lcs[i+1][j], lcs[i][j+1])
			}
		}
	}
	type op struct {
		kind byte // ' '、'-'、'+'
		i, j int  // 在 a、b 中的位置（下一行）
		text string
	}
	var ops []op
	i, j := 0, 0
	for i < n || j < m {
		switch {
		case i < n && j < m && a[i] == b[j]:
			ops = append(ops, op{' ', i, j, a[i]})
			i, j = i+1, j+1
		case i < n && (j == m || lcs[i+1][j] >= lcs[i][j+1]):
			ops = append(ops, op{'-', i, j, a[i]})
			i++
		default:
			ops = append(ops, op{'+', i, j, b[j]})
			j++
		}
	}
	var out []string
	for k := 0; k < len(ops); {
		if ops[k].kind == ' ' {
			k++
			continue
		}
		start := max(k-context, 0)
		end := k
		for end < len(ops) {
			if ops[end].kind != ' ' {
				end++
				continue
			}
			run := end
			for run < len(ops) && ops[run].kind == ' ' {
				run++
			}
			if run == len(ops) || run-end > 2*context {
				end = min(end+context, len(ops))
				break
			}
			end = run
		}
		if len(out) == 0 {
			out = append(out, "--- "+fromName, "+++ "+toName)
		}
		var aCount, bCount int
		for _, o := range ops[start:end] {
			if o.kind != '+' {
				aCount++
			}
			if o.kind != '-' {
				bCount++
			}
		}
		out = append(out, fmt.Sprintf("@@ -%s +%s @@", hunkRange(ops[start].i, aCount), hunkRange(ops[start].j, bCount)))
		for _, o := range ops[start:end] {
			out = append(out, string(o.kind)+o.text)
		}
		k = end
	}
	return out
}

func hunkRange(start, count int) string {
	if count == 1 {
		return fmt.Sprint(start + 1)
	}
	if count == 0 {
		return fmt.Sprintf("%d,0", start)
	}
	return fmt.Sprintf("%d,%d", start+1, count)
}
