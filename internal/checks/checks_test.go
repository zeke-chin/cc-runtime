package checks

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/zeke-chin/cc-runtime/internal/config"
	"github.com/zeke-chin/cc-runtime/internal/deploy"
	"github.com/zeke-chin/cc-runtime/internal/testutil"
)

func TestLayoutAndTrackedFilesAndVirtualenvs(t *testing.T) {
	base := t.TempDir()
	cfg := testutil.MakeConfig(t, base, "")
	if errs := LayoutErrors(cfg); len(errs) != 0 {
		t.Fatal(errs)
	}
	nested := testutil.MakeConfig(t, filepath.Join(base, "n"), "")
	nested.Runtime = filepath.Join(nested.Workspace, "inside")
	if len(LayoutErrors(nested)) == 0 {
		t.Fatal("运行目录在工作区里应报错")
	}
	os.Remove(filepath.Join(cfg.Root, config.ProjectIgnoresFile))
	if errs := LayoutErrors(cfg); len(errs) != 1 || !strings.Contains(errs[0], "sync-ignores") {
		t.Fatal(errs)
	}
	ignores := testutil.ProjectIgnores()
	app := filepath.Join(cfg.Workspace, "app")
	os.MkdirAll(filepath.Join(app, "target"), 0o755)
	testutil.Git(t, app, "init", "-q")
	os.WriteFile(filepath.Join(app, "target/keep.txt"), []byte("tracked\n"), 0o644)
	testutil.Git(t, app, "add", "-f", "target/keep.txt")
	testutil.Git(t, app, "commit", "-qm", "track ignored")
	if got := TrackedViolations(cfg.Workspace, ignores); !slices.Equal(got, []Violation{{"app", "target/keep.txt"}}) {
		t.Fatal(got)
	}
	for _, name := range []string{".venv-old", ".venv"} {
		os.Mkdir(filepath.Join(app, name), 0o755)
		os.WriteFile(filepath.Join(app, name, "pyvenv.cfg"), nil, 0o644)
	}
	if got := StrayVirtualenvs(cfg.Workspace, ignores); !slices.Equal(got, []string{"app/.venv-old"}) {
		t.Fatal(got)
	}
}

func TestContainerStateMounts(t *testing.T) {
	var c deploy.Container
	c.Name = "/db"
	for _, m := range []struct {
		Type, Source string
		RW           bool
	}{
		{"bind", "/w/app/data/pg", true},
		{"bind", "/w/app/schema.sql", false},
		{"bind", "/w/github_work/cc-runtime/tailscale/state", true},
		{"bind", "/w", true},
		{"volume", "/var/lib/docker/x", true},
	} {
		c.Mounts = append(c.Mounts, struct {
			Type        string `json:"Type"`
			Source      string `json:"Source"`
			Destination string `json:"Destination"`
			RW          bool   `json:"RW"`
		}{Type: m.Type, Source: m.Source, RW: m.RW})
	}
	saved := inspectAll
	defer func() { inspectAll = saved }()
	inspectAll = func() ([]deploy.Container, error) { return []deploy.Container{c}, nil }
	ignores := config.NewIgnoreList([]string{"/github_work/cc-runtime/tailscale/state/"})
	got, err := ContainerStateMounts("/w", ignores)
	if err != nil || !slices.Equal(got, []StateMount{{"db", "app/data/pg"}}) {
		t.Fatal(got, err)
	}
}

func TestProxyCheckOnRepositoryTemplate(t *testing.T) {
	dir := t.TempDir()
	template, err := os.ReadFile(filepath.Join(testutil.RepoRoot(), "proxy/config.example.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(dir, "config.example.yaml"), template, 0o644)
	os.WriteFile(filepath.Join(dir, "config.yaml"), template, 0o644)
	changed := strings.Replace(string(template), "mode: rule\n", "mode: rule\nextra: line\npassword: secret\n", 1)
	os.WriteFile(filepath.Join(dir, "config-other.yaml"), []byte(changed), 0o644)
	results, err := ProxyDifferences(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 2 || results[0].Name != "config-other.yaml" || results[1].Name != "config.yaml" || len(results[1].Lines) != 0 {
		t.Fatalf("%+v", results)
	}
	text := strings.Join(results[0].Lines, "\n")
	if !strings.Contains(text, "+extra: line") || strings.Contains(text, "secret") || !strings.Contains(text, "<已隐藏凭据行>") {
		t.Fatal(text)
	}
}

func TestUnifiedDiff(t *testing.T) {
	a := strings.Split("1\n2\n3\n4\n5\n6\n7\n8", "\n")
	b := strings.Split("1\n2\nx\n4\n5\n6\n7\n8\n9", "\n")
	got := strings.Join(UnifiedDiff(a, b, "a", "b", 1), "\n")
	want := "--- a\n+++ b\n@@ -2,3 +2,3 @@\n 2\n-3\n+x\n 4\n@@ -8 +8,2 @@\n 8\n+9"
	if got != want {
		t.Fatalf("得到\n%s", got)
	}
	if UnifiedDiff(a, a, "a", "b", 1) != nil {
		t.Fatal("相同时应为空")
	}
}
