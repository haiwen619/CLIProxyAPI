package accountpool

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExportConfigWritesOnlyEnabledAccounts(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	svc := New(root)

	doc := document{
		Version: storeVersion,
		Accounts: []Account{
			{
				ID:         "enabled-1",
				Email:      "enabled@example.com",
				Password:   "secret",
				TOTPSecret: "TOTP",
				Enabled:    true,
				CreatedAt:  "2026-03-26T00:00:00Z",
				UpdatedAt:  "2026-03-26T00:00:00Z",
			},
			{
				ID:        "disabled-1",
				Email:     "disabled@example.com",
				Password:  "hidden",
				Enabled:   false,
				CreatedAt: "2026-03-26T00:00:00Z",
				UpdatedAt: "2026-03-26T00:00:00Z",
			},
		},
	}

	svc.mu.Lock()
	if err := svc.saveLocked(doc); err != nil {
		svc.mu.Unlock()
		t.Fatalf("saveLocked: %v", err)
	}
	result, err := svc.exportConfigLocked()
	svc.mu.Unlock()
	if err != nil {
		t.Fatalf("exportConfigLocked: %v", err)
	}

	if result.Exported != 1 {
		t.Fatalf("expected 1 exported account, got %d", result.Exported)
	}

	raw, err := os.ReadFile(filepath.Join(root, "pythonLoginRpa", "config.json"))
	if err != nil {
		t.Fatalf("read export: %v", err)
	}

	var payload struct {
		Accounts []map[string]string `json:"accounts"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("parse export: %v", err)
	}
	if len(payload.Accounts) != 1 {
		t.Fatalf("expected 1 account in exported config, got %d", len(payload.Accounts))
	}
	if payload.Accounts[0]["email"] != "enabled@example.com" {
		t.Fatalf("unexpected email in export: %q", payload.Accounts[0]["email"])
	}
}

func TestImportConfigReplacesPool(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, "pythonLoginRpa")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	raw := []byte(`{"accounts":[{"email":"first@example.com","password":"p1","totp_secret":""},{"email":"second@example.com","password":"p2","totp_secret":"ABC"}]}`)
	if err := os.WriteFile(filepath.Join(configDir, "config.json"), raw, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	svc := New(root)
	svc.mu.Lock()
	doc, err := svc.importConfigLocked()
	svc.mu.Unlock()
	if err != nil {
		t.Fatalf("importConfigLocked: %v", err)
	}

	if len(doc.Accounts) != 2 {
		t.Fatalf("expected 2 imported accounts, got %d", len(doc.Accounts))
	}
	if !doc.Accounts[0].Enabled || !doc.Accounts[1].Enabled {
		t.Fatalf("expected imported accounts to be enabled by default")
	}
}

func TestPrepareReplacementAccountsRejectsEmptyInput(t *testing.T) {
	t.Parallel()

	accounts, err := prepareReplacementAccounts(nil, "2026-03-27T00:00:00Z")
	if err == nil {
		t.Fatal("expected error for empty input")
	}
	if accounts != nil {
		t.Fatalf("expected nil accounts on error, got %v", accounts)
	}
}

func TestArrayImportShapeCanBeNormalized(t *testing.T) {
	t.Parallel()

	raw := []byte(`[
  {
    "email": "ncarson697@ssd.baileybridge.org",
    "password": "xjDrkDjVA4znQ4q$",
    "registered_at": "2026-03-26T17:39:38.030395"
  },
  {
    "email": "tchapman943@dayhzuj.shop",
    "password": "DrTjSl!vtq4Uy90b",
    "registered_at": "2026-03-27T13:57:56.579971"
  }
]`)

	var imported []Account
	if err := json.Unmarshal(raw, &imported); err != nil {
		t.Fatalf("unmarshal array import: %v", err)
	}

	accounts, err := prepareReplacementAccounts(imported, "2026-03-27T00:00:00Z")
	if err != nil {
		t.Fatalf("prepareReplacementAccounts: %v", err)
	}
	if len(accounts) != 2 {
		t.Fatalf("expected 2 accounts, got %d", len(accounts))
	}
	if accounts[0].Email != "ncarson697@ssd.baileybridge.org" {
		t.Fatalf("unexpected first email: %q", accounts[0].Email)
	}
	if accounts[1].Password != "DrTjSl!vtq4Uy90b" {
		t.Fatalf("unexpected second password: %q", accounts[1].Password)
	}
}

func TestPythonExecEnvIncludesUnbufferedAndVirtualEnv(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	svc := New(root)
	pythonExec := filepath.Join(root, ".venv", "Scripts", "python.exe")
	env := svc.pythonExecEnv(pythonExec)

	joined := strings.Join(env, "\n")
	if !strings.Contains(joined, "PYTHONUNBUFFERED=1") {
		t.Fatalf("expected PYTHONUNBUFFERED=1 in environment")
	}
	if !strings.Contains(joined, "PYTHONIOENCODING=utf-8") {
		t.Fatalf("expected PYTHONIOENCODING=utf-8 in environment")
	}
	if !strings.Contains(joined, "VIRTUAL_ENV="+filepath.Join(root, ".venv")) {
		t.Fatalf("expected VIRTUAL_ENV to point to venv")
	}
	if !strings.Contains(joined, "PATH="+filepath.Join(root, ".venv", "Scripts")+string(os.PathListSeparator)) {
		t.Fatalf("expected PATH to be prefixed with venv Scripts directory")
	}
}
