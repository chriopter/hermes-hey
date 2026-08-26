package main

import (
	"context"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

func syntheticIdentity() *generated.Identity {
	return &generated.Identity{
		Id:       1,
		Accounts: []generated.Account{{Id: 7, Status: "active", Name: "Example"}},
		AllUsers: []generated.User{{Id: 8, AccountId: 7, Contact: generated.Contact{EmailAddress: "me@example.com"}}},
	}
}

func TestVerifyIdentityRequiresExactAccountAndEmail(t *testing.T) {
	if err := verifyIdentity(syntheticIdentity(), 7, "me@example.com"); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		account int64
		email   string
	}{{8, "me@example.com"}, {7, "other@example.com"}, {0, "me@example.com"}} {
		if err := verifyIdentity(syntheticIdentity(), tc.account, tc.email); err == nil {
			t.Fatalf("verifyIdentity(%d, %q) error = nil", tc.account, tc.email)
		}
	}
}

func TestVerifyResponsePinsProtocolAndSDKVersion(t *testing.T) {
	response := newVerifyResponse(7, "me@example.com")
	if !response.OK || response.ProtocolVersion != 1 || response.SDKVersion != "0.24.0" {
		t.Fatalf("verify response = %#v", response)
	}
}

func TestExitCodeRetriesOnlyExplicitSafeFailures(t *testing.T) {
	if got := exitCode(safeRetry(errors.New("synthetic preflight failure"))); got != 75 {
		t.Fatalf("safe retry exit code = %d, want 75", got)
	}
	if got := exitCode(errors.New("synthetic ambiguous failure")); got != 1 {
		t.Fatalf("ambiguous exit code = %d, want 1", got)
	}
}

func TestReplyClientPreflightRetriesOnlyTransientFailures(t *testing.T) {
	for name, transient := range map[string]error{
		"network":    hey.ErrNetwork(errors.New("offline")),
		"rate limit": hey.ErrRateLimit(1),
		"server":     &hey.Error{Code: hey.CodeAPI, HTTPStatus: 503, Message: "unavailable"},
	} {
		t.Run(name, func(t *testing.T) {
			if err := classifyReplyClientError(transient); !isSafeRetry(err) {
				t.Fatalf("classifyReplyClientError(%v) = %v, want safe retry", transient, err)
			}
		})
	}
	for name, permanent := range map[string]error{
		"authentication": hey.ErrAuth("invalid"),
		"forbidden":      hey.ErrForbidden("denied"),
		"not found":      &hey.Error{Code: hey.CodeNotFound, HTTPStatus: 404, Message: "missing"},
	} {
		t.Run(name, func(t *testing.T) {
			if err := classifyReplyClientError(permanent); isSafeRetry(err) {
				t.Fatalf("classifyReplyClientError(%v) = %v, must not retry", permanent, err)
			}
		})
	}
}

func TestValidateBaseURLAllowsHTTPSAndLocalTestServerOnly(t *testing.T) {
	for _, value := range []string{"https://app.hey.com", "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"} {
		if err := validateBaseURL(value); err != nil {
			t.Fatalf("validateBaseURL(%q) = %v", value, err)
		}
	}
	for _, value := range []string{"http://example.com", "ftp://example.com", "https://example.com/path", "https://user:pass@example.com"} {
		if err := validateBaseURL(value); err == nil {
			t.Fatalf("validateBaseURL(%q) error = nil", value)
		}
	}
}

func TestRedactedErrorDoesNotLeakSecretsURLsOrContent(t *testing.T) {
	err := errors.New("request https://example.com/private?token=secret failed: access_token=abc content=<p>customer text</p>")
	got := redactError(err)
	for _, forbidden := range []string{"secret", "abc", "customer", "example.com/private"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("redacted error %q contains %q", got, forbidden)
		}
	}
	if got == "" {
		t.Fatal("redacted error is empty")
	}
}

func TestAccountArgumentsRequireCanonicalPositiveInt64(t *testing.T) {
	const maxInt64 = "9223372036854775807"
	account, err := parseCanonicalAccount(maxInt64)
	if err != nil || account != 9223372036854775807 {
		t.Fatalf("parseCanonicalAccount(%q) = (%d, %v)", maxInt64, account, err)
	}

	invalid := []string{
		"", "0", "01", "+1", "-1", " 1", "1 ", "١", "1.0",
		"9223372036854775808", "9999999999999999999999999999999999999999",
	}
	modes := map[string]func(string) []string{
		"verify": func(value string) []string {
			return []string{"verify", "--account", value, "--own-email", "me@example.com", "--config-dir", t.TempDir()}
		},
		"watch": func(value string) []string {
			return []string{"watch", "--account", value, "--own-email", "me@example.com", "--config-dir", t.TempDir(), "--cursor-state", t.TempDir() + "/cursor", "--poll-interval", "1s"}
		},
		"reply": func(value string) []string {
			return []string{"reply", "--account", value, "--config-dir", t.TempDir(), "--thread-id", "1"}
		},
	}
	for _, value := range invalid {
		for mode, args := range modes {
			t.Run(mode+"/"+value, func(t *testing.T) {
				err := run(context.Background(), args(value), strings.NewReader(""), io.Discard)
				if err == nil {
					t.Fatal("run error = nil")
				}
				if got := redactError(err); got != "invalid arguments" {
					t.Fatalf("redactError(%v) = %q, want invalid arguments", err, got)
				}
			})
		}
	}
}
