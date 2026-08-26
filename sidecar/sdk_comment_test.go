package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

func TestSDKCreateCommentUsesOfficialFormEndpoint(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPost || r.URL.Path != "/topics/77/comments" {
			t.Fatalf("request = %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Content-Type"); got != "application/x-www-form-urlencoded" {
			t.Fatalf("content type = %q", got)
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatal(err)
		}
		if string(body) != "comment%5Bcontent%5D=Internal+response" {
			t.Fatalf("body = %q", body)
		}
		w.Header().Set("Location", "/topics/77")
		w.WriteHeader(http.StatusSeeOther)
	}))
	defer server.Close()

	cfg := hey.DefaultConfig()
	cfg.BaseURL = server.URL
	client := hey.NewClient(cfg, &hey.StaticTokenProvider{Token: "synthetic-token"}, hey.WithMaxRetries(0))
	if err := (sdkAdapter{client: client}).CreateComment(context.Background(), 77, "Internal response"); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want 1", calls)
	}
}

func TestSDKCreateCommentAcceptsSuccessfulNonRedirectResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	cfg := hey.DefaultConfig()
	cfg.BaseURL = server.URL
	client := hey.NewClient(cfg, &hey.StaticTokenProvider{Token: "synthetic-token"}, hey.WithMaxRetries(0))
	if err := (sdkAdapter{client: client}).CreateComment(context.Background(), 77, "Internal response"); err != nil {
		t.Fatal(err)
	}
}

func TestSDKCreateCommentRejectsUnexpectedRedirect(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Location", "/topics/88")
		w.WriteHeader(http.StatusFound)
	}))
	defer server.Close()

	cfg := hey.DefaultConfig()
	cfg.BaseURL = server.URL
	client := hey.NewClient(cfg, &hey.StaticTokenProvider{Token: "synthetic-token"}, hey.WithMaxRetries(0))
	err := (sdkAdapter{client: client}).CreateComment(context.Background(), 77, "Internal response")
	if err == nil || !strings.Contains(err.Error(), "redirect") {
		t.Fatalf("error = %v, want redirect rejection", err)
	}
}
