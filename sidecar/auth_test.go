package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type storedCredentialFixture struct {
	AccessToken   string `json:"access_token"`
	RefreshToken  string `json:"refresh_token"`
	ExpiresAt     int64  `json:"expires_at"`
	OAuthType     string `json:"oauth_type"`
	TokenEndpoint string `json:"token_endpoint"`
}

func writeCredentialFixture(t *testing.T, dir, origin string, credential storedCredentialFixture) {
	t.Helper()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(map[string]storedCredentialFixture{origin: credential})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "credentials.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestCredentialManagerRejectsInsecureCredentialDirectory(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX permission check")
	}
	dir := t.TempDir()
	writeCredentialFixture(t, dir, "https://example.test", storedCredentialFixture{AccessToken: "synthetic-access"})
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	manager := newCredentialManager(dir, "https://example.test", nil)
	if _, err := manager.AccessToken(context.Background()); err == nil {
		t.Fatal("AccessToken() error = nil, want insecure credential directory error")
	}
}

func TestCredentialManagerRejectsInsecureCredentialDirectoryBeforeLock(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX permission check")
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	manager := newCredentialManager(dir, "https://example.test", nil)
	if unlock, err := manager.lockCredentials(); err == nil {
		unlock()
		t.Fatal("lockCredentials() error = nil, want insecure credential directory error")
	}
}

func TestCredentialManagerRejectsCredentialFileWithoutExactOwnerReadWriteMode(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX permission check")
	}
	dir := t.TempDir()
	writeCredentialFixture(t, dir, "https://example.test", storedCredentialFixture{AccessToken: "synthetic-access"})
	if err := os.Chmod(filepath.Join(dir, "credentials.json"), 0o400); err != nil {
		t.Fatal(err)
	}
	manager := newCredentialManager(dir, "https://example.test", nil)
	if _, err := manager.AccessToken(context.Background()); err == nil || !strings.Contains(err.Error(), "credential file is insecure") {
		t.Fatalf("AccessToken() error = %v, want insecure credential file error", err)
	}
}

func TestCredentialManagerRefreshClearsExpiryForNonExpiringToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"non-expiring-access","expires_in":0}`))
	}))
	defer server.Close()

	dir := t.TempDir()
	writeCredentialFixture(t, dir, server.URL, storedCredentialFixture{
		AccessToken:   "expired-access",
		RefreshToken:  "synthetic-refresh",
		ExpiresAt:     1,
		TokenEndpoint: server.URL + "/oauth/token",
	})
	manager := newCredentialManager(dir, server.URL, server.Client())
	if _, err := manager.AccessToken(context.Background()); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatal(err)
	}
	var stored map[string]storedCredentialFixture
	if err := json.Unmarshal(data, &stored); err != nil {
		t.Fatal(err)
	}
	if got := stored[server.URL].ExpiresAt; got != 0 {
		t.Fatalf("ExpiresAt = %d, want 0", got)
	}
}

func TestCredentialManagerConcurrentRefreshUsesRotatedTokenOnce(t *testing.T) {
	var requests atomic.Int32
	firstEntered := make(chan struct{})
	secondEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		count := requests.Add(1)
		if count == 1 {
			close(firstEntered)
			<-releaseFirst
		} else if count == 2 {
			close(secondEntered)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"rotated-access","refresh_token":"rotated-refresh","expires_in":3600}`))
	}))
	defer server.Close()

	dir := t.TempDir()
	writeCredentialFixture(t, dir, server.URL, storedCredentialFixture{
		AccessToken:   "expired-access",
		RefreshToken:  "initial-refresh",
		ExpiresAt:     1,
		OAuthType:     "oauth",
		TokenEndpoint: server.URL + "/oauth/token",
	})
	managers := []*credentialManager{
		newCredentialManager(dir, server.URL, server.Client()),
		newCredentialManager(dir, server.URL, server.Client()),
	}
	results := make(chan string, 2)
	for index, manager := range managers {
		if index == 1 {
			<-firstEntered
		}
		go func() {
			token, err := manager.AccessToken(context.Background())
			if err != nil {
				results <- "error"
				return
			}
			results <- token
		}()
	}
	select {
	case <-secondEntered:
	case <-time.After(100 * time.Millisecond):
	}
	close(releaseFirst)
	first, second := <-results, <-results
	if first != "rotated-access" || second != "rotated-access" || requests.Load() != 1 {
		t.Fatalf("tokens=(%q,%q) refresh requests=%d", first, second, requests.Load())
	}
}

func TestCredentialManagerRefreshRejectsCrossOrigin307WithoutForwardingBody(t *testing.T) {
	var destinationRequests atomic.Int32
	var destinationBody string
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		destinationRequests.Add(1)
		body, _ := io.ReadAll(r.Body)
		destinationBody = string(body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"stolen"}`))
	}))
	defer destination.Close()

	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Location", destination.URL+"/steal")
		w.WriteHeader(http.StatusTemporaryRedirect)
	}))
	defer source.Close()

	dir := t.TempDir()
	writeCredentialFixture(t, dir, source.URL, storedCredentialFixture{
		AccessToken:   "expired-access",
		RefreshToken:  "synthetic-refresh",
		ExpiresAt:     1,
		TokenEndpoint: source.URL + "/oauth/token",
	})
	manager := newCredentialManager(dir, source.URL, source.Client())
	if _, err := manager.AccessToken(context.Background()); err == nil {
		t.Fatal("AccessToken() error = nil, want redirect rejection")
	}
	if got := destinationRequests.Load(); got != 0 {
		t.Fatalf("destination requests = %d, want 0; body = %q", got, destinationBody)
	}
}

func TestCredentialManagerRefreshRejectsHTTPSDowngrade308WithoutForwardingBody(t *testing.T) {
	var destinationRequests atomic.Int32
	var destinationBody string
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		destinationRequests.Add(1)
		body, _ := io.ReadAll(r.Body)
		destinationBody = string(body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"stolen"}`))
	}))
	defer destination.Close()

	source := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Location", destination.URL+"/steal")
		w.WriteHeader(http.StatusPermanentRedirect)
	}))
	defer source.Close()

	dir := t.TempDir()
	writeCredentialFixture(t, dir, source.URL, storedCredentialFixture{
		AccessToken:   "expired-access",
		RefreshToken:  "synthetic-refresh",
		ExpiresAt:     1,
		TokenEndpoint: source.URL + "/oauth/token",
	})
	manager := newCredentialManager(dir, source.URL, source.Client())
	if _, err := manager.AccessToken(context.Background()); err == nil {
		t.Fatal("AccessToken() error = nil, want redirect rejection")
	}
	if got := destinationRequests.Load(); got != 0 {
		t.Fatalf("destination requests = %d, want 0; body = %q", got, destinationBody)
	}
}

func TestCredentialManagerRefreshUsesCLIClientAndInstallIDAndPersistsRotation(t *testing.T) {
	var mu sync.Mutex
	form := make(map[string]string)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Error(err)
			return
		}
		mu.Lock()
		for key := range r.Form {
			form[key] = r.Form.Get(key)
		}
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"rotated-access","refresh_token":"rotated-refresh","expires_in":3600}`))
	}))
	defer server.Close()

	dir := t.TempDir()
	writeCredentialFixture(t, dir, server.URL, storedCredentialFixture{
		AccessToken:   "expired-access",
		RefreshToken:  "initial-refresh",
		ExpiresAt:     time.Now().Add(-time.Hour).Unix(),
		OAuthType:     "oauth",
		TokenEndpoint: server.URL + "/oauth/token",
	})
	manager := newCredentialManager(dir, server.URL, server.Client())
	token, err := manager.AccessToken(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if token != "rotated-access" {
		t.Fatalf("access token = %q", token)
	}
	mu.Lock()
	if form["grant_type"] != "refresh_token" || form["refresh_token"] != "initial-refresh" || form["client_id"] != cliOAuthClientID || form["install_id"] != cliInstallID {
		t.Fatalf("refresh form fields = %#v", form)
	}
	mu.Unlock()

	data, err := os.ReadFile(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatal(err)
	}
	var stored map[string]storedCredentialFixture
	if err := json.Unmarshal(data, &stored); err != nil {
		t.Fatal(err)
	}
	if stored[server.URL].AccessToken != "rotated-access" || stored[server.URL].RefreshToken != "rotated-refresh" || stored[server.URL].OAuthType != "oauth" {
		t.Fatalf("stored credential metadata was not preserved")
	}
}
