package accountpool

import (
	"bytes"
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/router-for-me/CLIProxyAPI/v6/internal/config"
)

const (
	storeVersion      = 1
	defaultConfigName = "config.json"
	poolFileName      = "account_pool.json"
	loginTimeout      = 20 * time.Minute
)

//go:embed web/*
var webFS embed.FS

type Account struct {
	ID         string   `json:"id"`
	Email      string   `json:"email"`
	Password   string   `json:"password"`
	TOTPSecret string   `json:"totp_secret"`
	Enabled    bool     `json:"enabled"`
	Tags       []string `json:"tags,omitempty"`
	Notes      string   `json:"notes,omitempty"`
	CreatedAt  string   `json:"created_at"`
	UpdatedAt  string   `json:"updated_at"`
}

type document struct {
	Version   int       `json:"version"`
	UpdatedAt string    `json:"updated_at"`
	Accounts  []Account `json:"accounts"`
}

type Summary struct {
	Total           int `json:"total"`
	Enabled         int `json:"enabled"`
	WithTOTP        int `json:"with_totp"`
	MissingPassword int `json:"missing_password"`
}

type stateResponse struct {
	Accounts   []Account `json:"accounts"`
	Summary    Summary   `json:"summary"`
	FilePath   string    `json:"file_path"`
	ConfigPath string    `json:"config_path"`
}

type replaceAccountsRequest struct {
	Accounts []Account `json:"accounts"`
}

type patchStatusRequest struct {
	Enabled *bool `json:"enabled"`
}

type importConfigAccount struct {
	Email      string `json:"email"`
	Password   string `json:"password"`
	TOTPSecret string `json:"totp_secret"`
}

type exportConfigResult struct {
	Path         string `json:"path"`
	Total        int    `json:"total"`
	Exported     int    `json:"exported"`
	Skipped      int    `json:"skipped"`
	UpdatedAtUTC string `json:"updated_at_utc"`
}

type authFileInfo struct {
	Name         string `json:"name"`
	Path         string `json:"path"`
	Type         string `json:"type"`
	Email        string `json:"email"`
	Size         int64  `json:"size"`
	ModTime      string `json:"modtime"`
	NewlyCreated bool   `json:"newly_created"`
	Updated      bool   `json:"updated"`
}

type runAccountResult struct {
	AccountID      string         `json:"account_id"`
	Email          string         `json:"email"`
	PythonExec     string         `json:"python_executable"`
	Success        bool           `json:"success"`
	ExitCode       int            `json:"exit_code"`
	StartedAt      string         `json:"started_at"`
	FinishedAt     string         `json:"finished_at"`
	DurationMS     int64          `json:"duration_ms"`
	Output         string         `json:"output"`
	AuthFiles      []authFileInfo `json:"auth_files"`
	Message        string         `json:"message"`
	TempConfigPath string         `json:"temp_config_path,omitempty"`
}

type streamEvent struct {
	Type             string            `json:"type"`
	Message          string            `json:"message,omitempty"`
	Chunk            string            `json:"chunk,omitempty"`
	PythonExecutable string            `json:"python_executable,omitempty"`
	Result           *runAccountResult `json:"result,omitempty"`
}

type Service struct {
	mu         sync.Mutex
	cfgMu      sync.RWMutex
	cfg        *config.Config
	rootDir    string
	filePath   string
	configPath string
}

func New(configFilePath string) *Service {
	rootDir := resolveRootDir(configFilePath)
	pythonDir := filepath.Join(rootDir, "pythonLoginRpa")
	return &Service{
		rootDir:    rootDir,
		filePath:   filepath.Join(pythonDir, poolFileName),
		configPath: filepath.Join(pythonDir, defaultConfigName),
	}
}

func (s *Service) SetConfig(cfg *config.Config) {
	s.cfgMu.Lock()
	defer s.cfgMu.Unlock()
	s.cfg = cfg
}

func resolveRootDir(configFilePath string) string {
	configFilePath = strings.TrimSpace(configFilePath)
	if configFilePath != "" {
		if info, err := os.Stat(configFilePath); err == nil {
			if info.IsDir() {
				return configFilePath
			}
			return filepath.Dir(configFilePath)
		}
		return filepath.Dir(configFilePath)
	}
	if wd, err := os.Getwd(); err == nil {
		return wd
	}
	return "."
}

func InjectManagementHTML(content []byte) []byte {
	const injectTag = `<script defer src="/account-pool/assets/inject.js"></script>`
	if bytes.Contains(content, []byte(injectTag)) {
		return content
	}

	bodyClose := []byte("</body>")
	if idx := bytes.LastIndex(content, bodyClose); idx >= 0 {
		var out bytes.Buffer
		out.Grow(len(content) + len(injectTag))
		out.Write(content[:idx])
		out.WriteString(injectTag)
		out.Write(bodyClose)
		out.Write(content[idx+len(bodyClose):])
		return out.Bytes()
	}

	out := make([]byte, 0, len(content)+len(injectTag))
	out = append(out, content...)
	out = append(out, injectTag...)
	return out
}

func (s *Service) ServePage(c *gin.Context) {
	s.serveEmbedded(c, "account-pool.html", "text/html; charset=utf-8")
}

func (s *Service) ServeAsset(c *gin.Context) {
	name := strings.TrimSpace(c.Param("name"))
	switch name {
	case "page.css":
		s.serveEmbedded(c, filepath.Join("web", name), "text/css; charset=utf-8")
	case "page.js", "inject.js":
		s.serveEmbedded(c, filepath.Join("web", name), "application/javascript; charset=utf-8")
	default:
		c.AbortWithStatus(http.StatusNotFound)
	}
}

func (s *Service) serveEmbedded(c *gin.Context, name string, contentType string) {
	data, err := fs.ReadFile(webFS, filepath.ToSlash(filepath.Join("web", filepath.Base(name))))
	if err != nil {
		c.AbortWithStatus(http.StatusNotFound)
		return
	}
	c.Data(http.StatusOK, contentType, data)
}

func (s *Service) GetState(c *gin.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.loadLocked()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, s.makeStateResponse(doc))
}

func (s *Service) CreateAccount(c *gin.Context) {
	var req Account
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.loadLocked()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	now := time.Now().UTC().Format(time.RFC3339)
	account := normalizeAccount(req, now, Account{})
	account.ID = uuid.NewString()
	account.CreatedAt = now
	account.UpdatedAt = now

	if err := validateAccount(account, doc.Accounts, ""); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	doc.Accounts = append(doc.Accounts, account)
	s.sortAccounts(doc.Accounts)
	if err := s.saveLocked(doc); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusCreated, s.makeStateResponse(doc))
}

func (s *Service) UpdateAccount(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing account id"})
		return
	}

	var req Account
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.loadLocked()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	index := slices.IndexFunc(doc.Accounts, func(item Account) bool { return item.ID == id })
	if index < 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "account not found"})
		return
	}

	now := time.Now().UTC().Format(time.RFC3339)
	updated := normalizeAccount(req, now, doc.Accounts[index])
	updated.ID = doc.Accounts[index].ID
	updated.CreatedAt = doc.Accounts[index].CreatedAt
	updated.UpdatedAt = now

	if err := validateAccount(updated, doc.Accounts, updated.ID); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	doc.Accounts[index] = updated
	s.sortAccounts(doc.Accounts)
	if err := s.saveLocked(doc); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, s.makeStateResponse(doc))
}

func (s *Service) ReplaceAccounts(c *gin.Context) {
	var req replaceAccountsRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now().UTC().Format(time.RFC3339)
	accounts := make([]Account, 0, len(req.Accounts))
	for _, item := range req.Accounts {
		normalized := normalizeAccount(item, now, Account{})
		if normalized.ID == "" {
			normalized.ID = uuid.NewString()
		}
		if normalized.CreatedAt == "" {
			normalized.CreatedAt = now
		}
		normalized.UpdatedAt = now
		accounts = append(accounts, normalized)
	}

	for _, item := range accounts {
		if err := validateAccount(item, accounts, item.ID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
	}

	doc := document{
		Version:   storeVersion,
		UpdatedAt: now,
		Accounts:  accounts,
	}
	s.sortAccounts(doc.Accounts)
	if err := s.saveLocked(doc); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, s.makeStateResponse(doc))
}

func (s *Service) PatchStatus(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing account id"})
		return
	}

	var req patchStatusRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if req.Enabled == nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing enabled field"})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.loadLocked()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	index := slices.IndexFunc(doc.Accounts, func(item Account) bool { return item.ID == id })
	if index < 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "account not found"})
		return
	}

	doc.Accounts[index].Enabled = *req.Enabled
	doc.Accounts[index].UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	s.sortAccounts(doc.Accounts)
	if err := s.saveLocked(doc); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, s.makeStateResponse(doc))
}

func (s *Service) DeleteAccount(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing account id"})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.loadLocked()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	index := slices.IndexFunc(doc.Accounts, func(item Account) bool { return item.ID == id })
	if index < 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "account not found"})
		return
	}

	doc.Accounts = append(doc.Accounts[:index], doc.Accounts[index+1:]...)
	if err := s.saveLocked(doc); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, s.makeStateResponse(doc))
}

func (s *Service) ImportFromConfig(c *gin.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.importConfigLocked()
	if err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, os.ErrNotExist) {
			status = http.StatusNotFound
		}
		c.JSON(status, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, s.makeStateResponse(doc))
}

func (s *Service) ExportToConfig(c *gin.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()

	result, err := s.exportConfigLocked()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, result)
}

func (s *Service) RunCodexLogin(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing account id"})
		return
	}

	account, err := s.findAccount(id)
	if err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, os.ErrNotExist) {
			status = http.StatusNotFound
		}
		c.JSON(status, gin.H{"error": err.Error()})
		return
	}
	if strings.TrimSpace(account.Email) == "" || strings.TrimSpace(account.Password) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "account is missing email or password"})
		return
	}

	cfg := s.currentConfig()
	if cfg == nil || cfg.Port <= 0 {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "server config is unavailable"})
		return
	}

	managementKey := extractManagementKey(c)
	if managementKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing management key in request"})
		return
	}

	startedAt := time.Now().UTC()
	before, _ := s.snapshotAuthFiles()
	template := s.loadConfigTemplate()
	tempPath, err := s.writeSingleAccountConfig(account, managementKey, template)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer os.Remove(tempPath)

	pythonExec := s.resolvePythonExecutable(template)
	exitCode, output, runErr := s.executeCodexBatchLogin(c.Request.Context(), pythonExec, tempPath)
	after, _ := s.snapshotAuthFiles()
	changed := diffAuthFiles(before, after)
	finishedAt := time.Now().UTC()

	result := runAccountResult{
		AccountID:      account.ID,
		Email:          account.Email,
		PythonExec:     pythonExec,
		Success:        runErr == nil && len(changed) > 0,
		ExitCode:       exitCode,
		StartedAt:      startedAt.Format(time.RFC3339),
		FinishedAt:     finishedAt.Format(time.RFC3339),
		DurationMS:     finishedAt.Sub(startedAt).Milliseconds(),
		Output:         output,
		AuthFiles:      changed,
		TempConfigPath: tempPath,
	}

	switch {
	case result.Success:
		result.Message = fmt.Sprintf("认证完成，获取到 %d 个认证文件", len(changed))
	case runErr != nil:
		result.Message = runErr.Error()
	case len(changed) == 0:
		result.Message = "脚本执行完成，但未检测到新增或更新的 Codex 认证文件"
	}

	c.JSON(http.StatusOK, result)
}

func (s *Service) RunCodexLoginStream(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing account id"})
		return
	}

	account, err := s.findAccount(id)
	if err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, os.ErrNotExist) {
			status = http.StatusNotFound
		}
		c.JSON(status, gin.H{"error": err.Error()})
		return
	}
	if strings.TrimSpace(account.Email) == "" || strings.TrimSpace(account.Password) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "account is missing email or password"})
		return
	}

	cfg := s.currentConfig()
	if cfg == nil || cfg.Port <= 0 {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "server config is unavailable"})
		return
	}

	managementKey := extractManagementKey(c)
	if managementKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing management key in request"})
		return
	}

	c.Header("Content-Type", "application/x-ndjson; charset=utf-8")
	c.Header("Cache-Control", "no-store")
	c.Header("X-Accel-Buffering", "no")
	c.Status(http.StatusOK)

	template := s.loadConfigTemplate()
	pythonExec := s.resolvePythonExecutable(template)
	if err := s.writeStreamEvent(c, streamEvent{
		Type:             "meta",
		Message:          fmt.Sprintf("开始执行账号 %s 的 Codex 认证登录", account.Email),
		PythonExecutable: pythonExec,
	}); err != nil {
		return
	}

	startedAt := time.Now().UTC()
	before, _ := s.snapshotAuthFiles()
	tempPath, err := s.writeSingleAccountConfig(account, managementKey, template)
	if err != nil {
		_ = s.writeStreamEvent(c, streamEvent{Type: "meta", Message: err.Error()})
		return
	}
	defer os.Remove(tempPath)

	sink := &streamingOutputWriter{
		emit: func(chunk string) error {
			return s.writeStreamEvent(c, streamEvent{Type: "output", Chunk: chunk})
		},
	}

	exitCode, output, runErr := s.executeCodexBatchLoginStreaming(c.Request.Context(), pythonExec, tempPath, sink)
	after, _ := s.snapshotAuthFiles()
	changed := diffAuthFiles(before, after)
	finishedAt := time.Now().UTC()

	result := runAccountResult{
		AccountID:      account.ID,
		Email:          account.Email,
		PythonExec:     pythonExec,
		Success:        runErr == nil && len(changed) > 0,
		ExitCode:       exitCode,
		StartedAt:      startedAt.Format(time.RFC3339),
		FinishedAt:     finishedAt.Format(time.RFC3339),
		DurationMS:     finishedAt.Sub(startedAt).Milliseconds(),
		Output:         output,
		AuthFiles:      changed,
		TempConfigPath: tempPath,
	}

	switch {
	case result.Success:
		result.Message = fmt.Sprintf("认证完成，获取到 %d 个认证文件", len(changed))
	case runErr != nil:
		result.Message = runErr.Error()
	case len(changed) == 0:
		result.Message = "脚本执行完成，但未检测到新增或更新的 Codex 认证文件"
	}

	_ = s.writeStreamEvent(c, streamEvent{
		Type:   "result",
		Result: &result,
	})
}

func (s *Service) importConfigLocked() (document, error) {
	data, err := os.ReadFile(s.configPath)
	if err != nil {
		return document{}, err
	}

	var payload struct {
		Accounts []importConfigAccount `json:"accounts"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return document{}, fmt.Errorf("parse %s: %w", s.configPath, err)
	}

	now := time.Now().UTC().Format(time.RFC3339)
	accounts := make([]Account, 0, len(payload.Accounts))
	for _, item := range payload.Accounts {
		acc := normalizeAccount(Account{
			Email:      item.Email,
			Password:   item.Password,
			TOTPSecret: item.TOTPSecret,
			Enabled:    true,
		}, now, Account{})
		acc.ID = uuid.NewString()
		acc.CreatedAt = now
		acc.UpdatedAt = now
		accounts = append(accounts, acc)
	}

	for _, item := range accounts {
		if err := validateAccount(item, accounts, item.ID); err != nil {
			return document{}, err
		}
	}

	doc := document{
		Version:   storeVersion,
		UpdatedAt: now,
		Accounts:  accounts,
	}
	s.sortAccounts(doc.Accounts)
	if err := s.saveLocked(doc); err != nil {
		return document{}, err
	}
	return doc, nil
}

func (s *Service) exportConfigLocked() (exportConfigResult, error) {
	doc, err := s.loadLocked()
	if err != nil {
		return exportConfigResult{}, err
	}

	payload := make(map[string]any)
	sourcePath := s.configPath
	if data, readErr := os.ReadFile(s.configPath); readErr == nil && len(bytes.TrimSpace(data)) > 0 {
		if err := json.Unmarshal(data, &payload); err != nil {
			return exportConfigResult{}, fmt.Errorf("parse %s: %w", s.configPath, err)
		}
	} else {
		sourcePath = filepath.Join(filepath.Dir(s.configPath), "config.example.json")
		if data, readErr := os.ReadFile(sourcePath); readErr == nil && len(bytes.TrimSpace(data)) > 0 {
			if err := json.Unmarshal(data, &payload); err != nil {
				return exportConfigResult{}, fmt.Errorf("parse %s: %w", sourcePath, err)
			}
		}
	}

	exportAccounts := make([]map[string]string, 0, len(doc.Accounts))
	skipped := 0
	for _, item := range doc.Accounts {
		if !item.Enabled {
			skipped++
			continue
		}
		exportAccounts = append(exportAccounts, map[string]string{
			"email":       item.Email,
			"password":    item.Password,
			"totp_secret": item.TOTPSecret,
		})
	}

	if payload == nil {
		payload = make(map[string]any)
	}
	if _, ok := payload["headless"]; !ok {
		payload["headless"] = true
	}
	if _, ok := payload["delay_between_accounts"]; !ok {
		payload["delay_between_accounts"] = 3
	}
	payload["accounts"] = exportAccounts

	if err := os.MkdirAll(filepath.Dir(s.configPath), 0o755); err != nil {
		return exportConfigResult{}, err
	}

	serialized, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return exportConfigResult{}, err
	}
	serialized = append(serialized, '\n')
	if err := os.WriteFile(s.configPath, serialized, 0o600); err != nil {
		return exportConfigResult{}, err
	}

	return exportConfigResult{
		Path:         s.configPath,
		Total:        len(doc.Accounts),
		Exported:     len(exportAccounts),
		Skipped:      skipped,
		UpdatedAtUTC: time.Now().UTC().Format(time.RFC3339),
	}, nil
}

func (s *Service) findAccount(id string) (Account, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	doc, err := s.loadLocked()
	if err != nil {
		return Account{}, err
	}
	index := slices.IndexFunc(doc.Accounts, func(item Account) bool { return item.ID == id })
	if index < 0 {
		return Account{}, os.ErrNotExist
	}
	return doc.Accounts[index], nil
}

func (s *Service) currentConfig() *config.Config {
	s.cfgMu.RLock()
	defer s.cfgMu.RUnlock()
	return s.cfg
}

func extractManagementKey(c *gin.Context) string {
	if c == nil {
		return ""
	}
	if authHeader := strings.TrimSpace(c.GetHeader("Authorization")); authHeader != "" {
		parts := strings.SplitN(authHeader, " ", 2)
		if len(parts) == 2 && strings.EqualFold(parts[0], "bearer") {
			return strings.TrimSpace(parts[1])
		}
		return authHeader
	}
	return strings.TrimSpace(c.GetHeader("X-Management-Key"))
}

func (s *Service) localManagementURL() (string, error) {
	cfg := s.currentConfig()
	if cfg == nil || cfg.Port <= 0 {
		return "", errors.New("server port is not configured")
	}
	scheme := "http"
	if cfg.TLS.Enable {
		scheme = "https"
	}
	return fmt.Sprintf("%s://127.0.0.1:%d", scheme, cfg.Port), nil
}

func (s *Service) loadConfigTemplate() map[string]any {
	payload := make(map[string]any)
	candidates := []string{
		s.configPath,
		filepath.Join(filepath.Dir(s.configPath), "config.example.json"),
	}

	for _, candidate := range candidates {
		data, err := os.ReadFile(candidate)
		if err != nil || len(bytes.TrimSpace(data)) == 0 {
			continue
		}
		if err := json.Unmarshal(data, &payload); err == nil {
			return payload
		}
	}
	return payload
}

func (s *Service) writeSingleAccountConfig(account Account, managementKey string, payload map[string]any) (string, error) {
	managementURL, err := s.localManagementURL()
	if err != nil {
		return "", err
	}

	if payload == nil {
		payload = make(map[string]any)
	}
	payload["mgmt_url"] = managementURL
	payload["mgmt_key"] = managementKey
	payload["accounts"] = []map[string]string{{
		"email":       account.Email,
		"password":    account.Password,
		"totp_secret": account.TOTPSecret,
	}}
	if _, ok := payload["headless"]; !ok {
		payload["headless"] = true
	}
	if _, ok := payload["delay_between_accounts"]; !ok {
		payload["delay_between_accounts"] = 0
	}

	pythonDir := filepath.Dir(s.configPath)
	if err := os.MkdirAll(pythonDir, 0o755); err != nil {
		return "", err
	}
	tempFile, err := os.CreateTemp(pythonDir, "account-pool-run-*.json")
	if err != nil {
		return "", err
	}
	defer tempFile.Close()

	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return "", err
	}
	data = append(data, '\n')
	if _, err := tempFile.Write(data); err != nil {
		return "", err
	}
	return tempFile.Name(), nil
}

func (s *Service) executeCodexBatchLogin(parent context.Context, pythonExec string, tempPath string) (int, string, error) {
	ctx := parent
	if ctx == nil {
		ctx = context.Background()
	}
	var cancel context.CancelFunc
	ctx, cancel = context.WithTimeout(ctx, loginTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, pythonExec, "-u", "codex_batch_login.py", filepath.Base(tempPath))
	cmd.Dir = filepath.Dir(s.configPath)
	cmd.Env = s.pythonExecEnv(pythonExec)

	output, err := cmd.CombinedOutput()
	text := string(output)
	if ctx.Err() == context.DeadlineExceeded {
		return -1, text, fmt.Errorf("python codex_batch_login.py timed out after %s", loginTimeout)
	}
	if err == nil {
		return 0, text, nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode(), text, err
	}
	return -1, text, err
}

func (s *Service) executeCodexBatchLoginStreaming(parent context.Context, pythonExec string, tempPath string, sink *streamingOutputWriter) (int, string, error) {
	ctx := parent
	if ctx == nil {
		ctx = context.Background()
	}
	var cancel context.CancelFunc
	ctx, cancel = context.WithTimeout(ctx, loginTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, pythonExec, "-u", "codex_batch_login.py", filepath.Base(tempPath))
	cmd.Dir = filepath.Dir(s.configPath)
	cmd.Env = s.pythonExecEnv(pythonExec)
	cmd.Stdout = sink
	cmd.Stderr = sink

	err := cmd.Run()
	output := sink.String()
	if ctx.Err() == context.DeadlineExceeded {
		return -1, output, fmt.Errorf("python codex_batch_login.py timed out after %s", loginTimeout)
	}
	if err == nil {
		return 0, output, nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode(), output, err
	}
	return -1, output, err
}

func (s *Service) resolvePythonExecutable(template map[string]any) string {
	if template != nil {
		for _, key := range []string{"python_executable", "venv_python", "python_exec"} {
			if value, ok := template[key]; ok {
				if resolved := resolvePythonCandidate(fmt.Sprint(value)); resolved != "" {
					return resolved
				}
			}
		}
	}

	for _, candidate := range []string{
		os.Getenv("ACCOUNT_POOL_PYTHON"),
		filepath.Join("h:\\katu\\OpenailoginRpa", ".venv", "Scripts", "python.exe"),
		filepath.Join(filepath.Dir(s.configPath), ".venv", "Scripts", "python.exe"),
		filepath.Join(s.rootDir, ".venv", "Scripts", "python.exe"),
		"python",
	} {
		if resolved := resolvePythonCandidate(candidate); resolved != "" {
			return resolved
		}
	}
	return "python"
}

func resolvePythonCandidate(candidate string) string {
	candidate = strings.TrimSpace(candidate)
	if candidate == "" {
		return ""
	}
	if strings.ContainsAny(candidate, `\/:`) {
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		return ""
	}
	if resolved, err := exec.LookPath(candidate); err == nil {
		return resolved
	}
	return ""
}

func (s *Service) pythonExecEnv(pythonExec string) []string {
	env := append([]string{}, os.Environ()...)
	env = append(env, "PYTHONIOENCODING=utf-8")
	env = append(env, "PYTHONUNBUFFERED=1")

	lower := strings.ToLower(strings.TrimSpace(pythonExec))
	if strings.HasSuffix(lower, "python.exe") || strings.HasSuffix(lower, "/python") || strings.HasSuffix(lower, "\\python") {
		scriptsDir := filepath.Dir(pythonExec)
		venvDir := filepath.Dir(scriptsDir)
		env = append(env, "VIRTUAL_ENV="+venvDir)
		env = append(env, "PATH="+scriptsDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	return env
}

func (s *Service) writeStreamEvent(c *gin.Context, event streamEvent) error {
	if c == nil {
		return errors.New("nil context")
	}
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if _, err := c.Writer.Write(append(data, '\n')); err != nil {
		return err
	}
	if flusher, ok := c.Writer.(http.Flusher); ok {
		flusher.Flush()
	}
	return nil
}

type streamingOutputWriter struct {
	mu   sync.Mutex
	buf  bytes.Buffer
	emit func(string) error
}

func (w *streamingOutputWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	n, err := w.buf.Write(p)
	if err != nil {
		return n, err
	}
	if w.emit != nil && n > 0 {
		if emitErr := w.emit(string(p[:n])); emitErr != nil {
			return n, emitErr
		}
	}
	return n, nil
}

func (w *streamingOutputWriter) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.buf.String()
}

func (s *Service) snapshotAuthFiles() (map[string]authFileInfo, error) {
	cfg := s.currentConfig()
	if cfg == nil || strings.TrimSpace(cfg.AuthDir) == "" {
		return map[string]authFileInfo{}, nil
	}

	entries, err := os.ReadDir(cfg.AuthDir)
	if err != nil {
		return nil, err
	}

	files := make(map[string]authFileInfo)
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasSuffix(strings.ToLower(name), ".json") {
			continue
		}

		fullPath := filepath.Join(cfg.AuthDir, name)
		info, err := entry.Info()
		if err != nil {
			continue
		}

		summary := authFileInfo{
			Name:    name,
			Path:    fullPath,
			Size:    info.Size(),
			ModTime: info.ModTime().UTC().Format(time.RFC3339),
		}

		if data, err := os.ReadFile(fullPath); err == nil {
			var meta struct {
				Type  string `json:"type"`
				Email string `json:"email"`
			}
			if json.Unmarshal(data, &meta) == nil {
				summary.Type = strings.TrimSpace(meta.Type)
				summary.Email = strings.TrimSpace(meta.Email)
			}
		}

		if summary.Type == "" && !strings.Contains(strings.ToLower(name), "codex") {
			continue
		}
		if summary.Type != "" && !strings.EqualFold(summary.Type, "codex") {
			continue
		}

		files[summary.Path] = summary
	}
	return files, nil
}

func diffAuthFiles(before, after map[string]authFileInfo) []authFileInfo {
	changed := make([]authFileInfo, 0)
	for path, current := range after {
		prev, exists := before[path]
		if !exists {
			current.NewlyCreated = true
			changed = append(changed, current)
			continue
		}
		if prev.ModTime != current.ModTime || prev.Size != current.Size {
			current.Updated = true
			changed = append(changed, current)
		}
	}

	sort.SliceStable(changed, func(i, j int) bool {
		if changed[i].NewlyCreated != changed[j].NewlyCreated {
			return changed[i].NewlyCreated
		}
		return changed[i].ModTime > changed[j].ModTime
	})
	return changed
}

func (s *Service) makeStateResponse(doc document) stateResponse {
	return stateResponse{
		Accounts:   doc.Accounts,
		Summary:    summarize(doc.Accounts),
		FilePath:   s.filePath,
		ConfigPath: s.configPath,
	}
}

func summarize(accounts []Account) Summary {
	var summary Summary
	summary.Total = len(accounts)
	for _, item := range accounts {
		if item.Enabled {
			summary.Enabled++
		}
		if strings.TrimSpace(item.TOTPSecret) != "" {
			summary.WithTOTP++
		}
		if strings.TrimSpace(item.Password) == "" {
			summary.MissingPassword++
		}
	}
	return summary
}

func normalizeAccount(input Account, now string, base Account) Account {
	out := base
	out.Email = strings.TrimSpace(input.Email)
	out.Password = strings.TrimSpace(input.Password)
	out.TOTPSecret = strings.TrimSpace(input.TOTPSecret)
	out.Enabled = input.Enabled
	if input.ID != "" {
		out.ID = strings.TrimSpace(input.ID)
	}
	if input.CreatedAt != "" {
		out.CreatedAt = input.CreatedAt
	}
	if input.UpdatedAt != "" {
		out.UpdatedAt = input.UpdatedAt
	}
	out.Tags = normalizeTags(input.Tags)
	out.Notes = strings.TrimSpace(input.Notes)
	if out.CreatedAt == "" {
		out.CreatedAt = now
	}
	if out.UpdatedAt == "" {
		out.UpdatedAt = now
	}
	return out
}

func normalizeTags(tags []string) []string {
	normalized := make([]string, 0, len(tags))
	seen := make(map[string]struct{}, len(tags))
	for _, item := range tags {
		tag := strings.TrimSpace(item)
		if tag == "" {
			continue
		}
		key := strings.ToLower(tag)
		if _, exists := seen[key]; exists {
			continue
		}
		seen[key] = struct{}{}
		normalized = append(normalized, tag)
	}
	return normalized
}

func validateAccount(account Account, accounts []Account, currentID string) error {
	if strings.TrimSpace(account.Email) == "" {
		return errors.New("email is required")
	}

	emailKey := strings.ToLower(strings.TrimSpace(account.Email))
	for _, item := range accounts {
		if item.ID == currentID {
			continue
		}
		if strings.ToLower(strings.TrimSpace(item.Email)) == emailKey {
			return fmt.Errorf("duplicate email: %s", account.Email)
		}
	}
	return nil
}

func (s *Service) loadLocked() (document, error) {
	if data, err := os.ReadFile(s.filePath); err == nil {
		if len(bytes.TrimSpace(data)) == 0 {
			return document{Version: storeVersion, Accounts: []Account{}}, nil
		}

		var doc document
		if err := json.Unmarshal(data, &doc); err != nil {
			return document{}, fmt.Errorf("parse %s: %w", s.filePath, err)
		}
		if doc.Version == 0 {
			doc.Version = storeVersion
		}
		now := time.Now().UTC().Format(time.RFC3339)
		for i := range doc.Accounts {
			if doc.Accounts[i].ID == "" {
				doc.Accounts[i].ID = uuid.NewString()
			}
			if doc.Accounts[i].CreatedAt == "" {
				doc.Accounts[i].CreatedAt = now
			}
			if doc.Accounts[i].UpdatedAt == "" {
				doc.Accounts[i].UpdatedAt = doc.Accounts[i].CreatedAt
			}
			doc.Accounts[i].Email = strings.TrimSpace(doc.Accounts[i].Email)
			doc.Accounts[i].Password = strings.TrimSpace(doc.Accounts[i].Password)
			doc.Accounts[i].TOTPSecret = strings.TrimSpace(doc.Accounts[i].TOTPSecret)
			doc.Accounts[i].Tags = normalizeTags(doc.Accounts[i].Tags)
			doc.Accounts[i].Notes = strings.TrimSpace(doc.Accounts[i].Notes)
		}
		s.sortAccounts(doc.Accounts)
		return doc, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return document{}, err
	}

	return document{
		Version:   storeVersion,
		UpdatedAt: "",
		Accounts:  []Account{},
	}, nil
}

func (s *Service) saveLocked(doc document) error {
	doc.Version = storeVersion
	doc.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	if doc.Accounts == nil {
		doc.Accounts = []Account{}
	}

	if err := os.MkdirAll(filepath.Dir(s.filePath), 0o755); err != nil {
		return err
	}

	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(s.filePath, data, 0o600)
}

func (s *Service) sortAccounts(accounts []Account) {
	sort.SliceStable(accounts, func(i, j int) bool {
		left := accounts[i]
		right := accounts[j]
		if left.Enabled != right.Enabled {
			return left.Enabled
		}
		if left.UpdatedAt != right.UpdatedAt {
			return left.UpdatedAt > right.UpdatedAt
		}
		return strings.ToLower(left.Email) < strings.ToLower(right.Email)
	})
}
