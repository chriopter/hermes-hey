package main

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"

	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type fakeCommentAPI struct {
	calls []commentCall
	err   error
}

type commentCall struct {
	threadID int64
	content  string
}

func (f *fakeCommentAPI) CreateComment(_ context.Context, threadID int64, content string) error {
	f.calls = append(f.calls, commentCall{threadID: threadID, content: content})
	return f.err
}

func TestCommentCreatesExactlyOneCollabNote(t *testing.T) {
	api := &fakeCommentAPI{}
	var out bytes.Buffer
	if err := runComment(context.Background(), api, 77, strings.NewReader(`{"content":"Internal response"}`), &out); err != nil {
		t.Fatal(err)
	}
	if len(api.calls) != 1 || api.calls[0].threadID != 77 || api.calls[0].content != "Internal response" {
		t.Fatalf("CreateComment calls = %#v", api.calls)
	}
	if strings.TrimSpace(out.String()) != `{"ok":true}` {
		t.Fatalf("output = %q", out.String())
	}
}

func TestCommentMutationIsNotRetriedAfterAmbiguousFailure(t *testing.T) {
	api := &fakeCommentAPI{err: errors.New("synthetic ambiguous mutation failure")}
	err := runComment(context.Background(), api, 77, strings.NewReader(`{"content":"Internal response"}`), &bytes.Buffer{})
	if err == nil || isSafeRetry(err) || len(api.calls) != 1 {
		t.Fatalf("error = %v, safe_retry = %t, calls = %d", err, isSafeRetry(err), len(api.calls))
	}
}

func TestCommentDefinitiveRejectionIsRetryable(t *testing.T) {
	api := &fakeCommentAPI{err: hey.ErrValidation("not ready")}
	err := runComment(context.Background(), api, 77, strings.NewReader(`{"content":"Internal response"}`), &bytes.Buffer{})
	if !isSafeRetry(err) || len(api.calls) != 1 {
		t.Fatalf("error = %v, calls = %d, want safe retry", err, len(api.calls))
	}
}

func TestCommentRejectsMalformedOrEmptyInputBeforeMutation(t *testing.T) {
	for _, input := range []string{`{`, `{}`, `{"content":""}`, `{"content":"ok","extra":true}`} {
		api := &fakeCommentAPI{}
		if err := runComment(context.Background(), api, 77, strings.NewReader(input), &bytes.Buffer{}); err == nil {
			t.Fatalf("input %q error = nil", input)
		}
		if len(api.calls) != 0 {
			t.Fatalf("input %q made a mutation", input)
		}
	}
}
