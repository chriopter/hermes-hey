package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

func TestSDKCredentialFileAndAccountScope(t *testing.T) {
	t.Setenv("HEY_NO_KEYRING", "1")
	var scoped bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/token" {
			if err := r.ParseForm(); err != nil || r.Form.Get("client_id") != cliOAuthClientID || r.Form.Get("install_id") != cliInstallID {
				http.Error(w, "invalid refresh client", http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"access_token":"rotated-token","refresh_token":"rotated-refresh","expires_in":3600}`))
			return
		}
		if r.Header.Get("Authorization") != "Bearer rotated-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/identity.json":
			_, _ = w.Write([]byte(`{"id":1,"accounts":[{"id":7,"status":"active"}],"all_users":[{"id":8,"account_id":7,"contact":{"email_address":"me@example.com"}}],"senders":[{"id":9,"account_id":7,"default":true}]}`))
		case "/boxes.json":
			scoped = r.URL.Query().Get("filtered_account_id") == "7"
			_, _ = w.Write([]byte(`[]`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	credentials := fmt.Sprintf(`{"%s":{"access_token":"expired-token","refresh_token":"synthetic-refresh","expires_at":1,"oauth_type":"oauth","token_endpoint":"%s/oauth/token"}}`, server.URL, server.URL)
	if err := os.WriteFile(filepath.Join(dir, "credentials.json"), []byte(credentials), 0o600); err != nil {
		t.Fatal(err)
	}
	_, client, identity, err := newSDKClients(context.Background(), 7, dir, server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyIdentity(identity, 7, "me@example.com"); err != nil {
		t.Fatal(err)
	}
	if _, err := (sdkAdapter{client: client}).ListBoxes(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !scoped {
		t.Fatal("account-scoped client did not send filtered_account_id=7")
	}
}

func TestSDKAdapterCommentBeforeLaterImageMessage(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/topics/31/entries.json":
			if r.URL.Query().Get("page") == "next" {
				_, _ = w.Write([]byte(`[{"id":101,"kind":"message"}]`))
				return
			}
			w.Header().Set("Link", fmt.Sprintf(`<%s/topics/31/entries?page=next>; rel="next"`, serverURL(r)))
			_, _ = w.Write([]byte(`[{"id":303,"kind":"message"},{"id":202,"kind":"comment"}]`))
		case "/messages/303.json":
			_, _ = w.Write([]byte(`{"id":303,"content":"<p>Later mail</p><img src=\"https://example.com/image.png\">","creator":{"id":9,"name":"Sender","email_address":"sender@example.com"},"created_at":"2026-01-02T03:04:05Z"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := hey.NewClient(&hey.Config{BaseURL: server.URL}, &hey.StaticTokenProvider{Token: "synthetic-token"}, hey.WithMaxRetries(0))
	adapter := sdkAdapter{client: client}
	ev, err := hydratePosting(context.Background(), adapter, generated.Posting{Id: 1, AccountId: 7, Kind: "topic", AppUrl: "https://app.hey.com/topics/31", VisibleEntryCount: 3}, "imbox")
	if err != nil {
		t.Fatal(err)
	}
	if ev == nil || ev.EntryID != 303 || !strings.Contains(ev.Content, "image.png") {
		t.Fatalf("event = %#v", ev)
	}
	mu.Lock()
	defer mu.Unlock()
	for _, path := range paths {
		if strings.Contains(path, "202") {
			t.Fatalf("historical comment was hydrated: %v", paths)
		}
	}
}

func TestSDKReplyUsesPrefillAndDoesNotRetryMutation(t *testing.T) {
	var postCount int
	var posted map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/topics/77/entries.json":
			_, _ = w.Write([]byte(`[{"id":303,"kind":"comment"},{"id":202,"kind":"message"}]`))
		case r.Method == http.MethodGet && r.URL.Path == "/entries/202/replies/new.json":
			_, _ = w.Write([]byte(`{"addressed":{"directly":[{"email_address":"to@example.com"}],"copied":[{"email_address":"cc@example.com"}]}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/identity.json":
			_, _ = w.Write([]byte(`{"id":1,"senders":[{"id":9,"account_id":7,"default":true,"email_address":"me@example.com"}]}`))
		case r.Method == http.MethodPost && r.URL.Path == "/entries/202/replies.json":
			postCount++
			_ = json.NewDecoder(r.Body).Decode(&posted)
			http.Error(w, `synthetic`, http.StatusInternalServerError)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := hey.NewClient(&hey.Config{BaseURL: server.URL}, &hey.StaticTokenProvider{Token: "synthetic-token"}, hey.WithMaxRetries(3))
	err := runReply(context.Background(), sdkAdapter{client: client}, 77, strings.NewReader(`{"content":"safe reply"}`), &bytes.Buffer{})
	if err == nil {
		t.Fatal("runReply error = nil")
	}
	if postCount != 1 {
		t.Fatalf("POST count = %d, want 1", postCount)
	}
	entry, ok := posted["entry"].(map[string]any)
	if !ok {
		t.Fatalf("posted body = %#v", posted)
	}
	addressed, ok := entry["addressed"].(map[string]any)
	if !ok || len(addressed["directly"].([]any)) != 1 || len(addressed["copied"].([]any)) != 1 {
		t.Fatalf("posted recipients = %#v", posted)
	}
}

func TestSDKReplyClassifiesTransientSenderPreflightSafeRetry(t *testing.T) {
	var postCount int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/topics/77/entries.json":
			_, _ = w.Write([]byte(`[{"id":202,"kind":"message"}]`))
		case r.Method == http.MethodGet && r.URL.Path == "/entries/202/replies/new.json":
			_, _ = w.Write([]byte(`{"addressed":{"directly":[{"email_address":"to@example.com"}]}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/identity.json":
			http.Error(w, `synthetic sender lookup failure`, http.StatusServiceUnavailable)
		case r.Method == http.MethodPost && r.URL.Path == "/entries/202/replies.json":
			postCount++
			w.WriteHeader(http.StatusCreated)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := hey.NewClient(&hey.Config{BaseURL: server.URL}, &hey.StaticTokenProvider{Token: "synthetic-token"}, hey.WithMaxRetries(0))

	err := runReply(context.Background(), sdkAdapter{client: client}, 77, strings.NewReader(`{"content":"safe reply"}`), &bytes.Buffer{})
	if !isSafeRetry(err) {
		t.Fatalf("sender preflight error = %v, want safe retry (exit 75)", err)
	}
	if postCount != 0 {
		t.Fatalf("POST count = %d, want zero before sender resolution", postCount)
	}
}

func serverURL(r *http.Request) string {
	return "http://" + r.Host
}
