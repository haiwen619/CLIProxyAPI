package auth

import (
	"context"
	"net/http"
	"testing"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy/executor"
)

type invalidatedRefreshExecutor struct{}

func (invalidatedRefreshExecutor) Identifier() string { return "codex" }

func (invalidatedRefreshExecutor) Execute(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, nil
}

func (invalidatedRefreshExecutor) ExecuteStream(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	return nil, nil
}

func (invalidatedRefreshExecutor) Refresh(context.Context, *Auth) (*Auth, error) {
	return nil, refreshStatusError{
		status: http.StatusUnauthorized,
		msg:    "401 Your authentication token has been invalidated. Please try signing in again.",
	}
}

func (invalidatedRefreshExecutor) CountTokens(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, nil
}

func (invalidatedRefreshExecutor) HttpRequest(context.Context, *Auth, *http.Request) (*http.Response, error) {
	return nil, nil
}

type refreshStatusError struct {
	status int
	msg    string
}

func (e refreshStatusError) Error() string   { return e.msg }
func (e refreshStatusError) StatusCode() int { return e.status }

func TestManagerRefreshAuthDisablesInvalidatedTokenAuth(t *testing.T) {
	t.Parallel()

	manager := NewManager(nil, nil, nil)
	manager.RegisterExecutor(invalidatedRefreshExecutor{})

	auth := &Auth{
		ID:       "refresh-invalidated-auth",
		Provider: "codex",
	}
	if _, errRegister := manager.Register(context.Background(), auth); errRegister != nil {
		t.Fatalf("register auth: %v", errRegister)
	}

	manager.refreshAuth(context.Background(), auth.ID)

	updated, ok := manager.GetByID(auth.ID)
	if !ok || updated == nil {
		t.Fatal("updated auth not found")
	}
	if !updated.Disabled {
		t.Fatal("expected auth to be disabled")
	}
	if updated.Status != StatusDisabled {
		t.Fatalf("auth status = %q, want %q", updated.Status, StatusDisabled)
	}
	if updated.StatusMessage != "disabled: authentication token invalidated" {
		t.Fatalf("status message = %q", updated.StatusMessage)
	}
	if updated.NextRefreshAfter != (time.Time{}) {
		t.Fatalf("next refresh after = %v, want zero", updated.NextRefreshAfter)
	}
}
