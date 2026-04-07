package auth

import (
	"net/http"
	"strings"
)

func shouldDisableAuthForInvalidatedToken(statusCode int, body string) bool {
	if statusCode != http.StatusUnauthorized {
		return false
	}
	lower := strings.ToLower(strings.TrimSpace(body))
	if lower == "" {
		return false
	}
	return strings.Contains(lower, "authentication token has been invalidated")
}
