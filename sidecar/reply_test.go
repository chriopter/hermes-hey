package main

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"

	generated "github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type fakeReplyAPI struct {
	entries     []generated.Entry
	entriesErr  error
	prefill     *generated.MessageDraft
	prefillErr  error
	senderID    int64
	senderErr   error
	createErr   error
	newReplyIDs []int64
	creates     []replyCall
}

type replyCall struct {
	entryID int64
	content string
	to      []string
	cc      []string
	bcc     []string
}

func (f *fakeReplyAPI) TopicEntries(context.Context, int64) ([]generated.Entry, error) {
	return f.entries, f.entriesErr
}
func (f *fakeReplyAPI) NewReply(_ context.Context, id int64) (*generated.MessageDraft, error) {
	f.newReplyIDs = append(f.newReplyIDs, id)
	return f.prefill, f.prefillErr
}
func (f *fakeReplyAPI) DefaultSenderID(context.Context) (int64, error) {
	return f.senderID, f.senderErr
}
func (f *fakeReplyAPI) CreateReply(_ context.Context, id int64, content string, to, cc, bcc []string) error {
	f.creates = append(f.creates, replyCall{id, content, to, cc, bcc})
	return f.createErr
}

func TestReplyUsesNewestMessageAndServerRecipients(t *testing.T) {
	api := &fakeReplyAPI{
		entries: []generated.Entry{{Id: 303, Kind: "comment"}, {Id: 202, Kind: "message"}, {Id: 101, Kind: "message"}},
		prefill: &generated.MessageDraft{Addressed: generated.Addressed{
			Directly:    []generated.Contact{{EmailAddress: "to@example.com"}},
			Copied:      []generated.Contact{{EmailAddress: "cc@example.com"}},
			Blindcopied: []generated.Contact{{EmailAddress: "bcc@example.com"}},
		}},
		senderID: 9,
	}
	var out bytes.Buffer
	if err := runReply(context.Background(), api, 77, strings.NewReader(`{"content":"<p>Reply</p>"}`), &out); err != nil {
		t.Fatal(err)
	}
	if len(api.newReplyIDs) != 1 || api.newReplyIDs[0] != 202 {
		t.Fatalf("NewReply IDs = %v, want only newest message 202", api.newReplyIDs)
	}
	if len(api.creates) != 1 || api.creates[0].entryID != 202 || api.creates[0].content != "<p>Reply</p>" {
		t.Fatalf("CreateReply calls = %#v", api.creates)
	}
	call := api.creates[0]
	if strings.Join(call.to, ",") != "to@example.com" || strings.Join(call.cc, ",") != "cc@example.com" || strings.Join(call.bcc, ",") != "bcc@example.com" {
		t.Fatalf("recipients = %#v", call)
	}
	if strings.TrimSpace(out.String()) != `{"ok":true}` {
		t.Fatalf("output = %q", out.String())
	}
}

func TestReplyMutationIsNotRetried(t *testing.T) {
	api := &fakeReplyAPI{entries: []generated.Entry{{Id: 202, Kind: "message"}}, prefill: &generated.MessageDraft{Addressed: generated.Addressed{Directly: []generated.Contact{{EmailAddress: "to@example.com"}}}}, senderID: 9, createErr: errors.New("synthetic failure")}
	err := runReply(context.Background(), api, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{})
	if err == nil || len(api.creates) != 1 {
		t.Fatalf("error = %v, create calls = %d, want one failed mutation", err, len(api.creates))
	}
}

func TestReplyRejectsZeroDefaultSenderBeforeCreateWithoutSafeRetry(t *testing.T) {
	api := &fakeReplyAPI{
		entries: []generated.Entry{{Id: 202, Kind: "message"}},
		prefill: &generated.MessageDraft{Addressed: generated.Addressed{
			Directly: []generated.Contact{{EmailAddress: "to@example.com"}},
		}},
		senderID: 0,
	}
	err := runReply(context.Background(), api, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{})
	if err == nil {
		t.Fatal("zero default sender error = nil")
	}
	if isSafeRetry(err) {
		t.Fatalf("zero default sender error = %v, must not be safe-retried", err)
	}
	if len(api.creates) != 0 {
		t.Fatalf("CreateReply calls = %d, want 0", len(api.creates))
	}
}

func TestReplyClassifiesOnlyPreMutationFailuresRetryable(t *testing.T) {
	beforePost := &fakeReplyAPI{entriesErr: hey.ErrNetwork(errors.New("synthetic read failure"))}
	err := runReply(context.Background(), beforePost, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{})
	if !isSafeRetry(err) {
		t.Fatalf("pre-mutation error = %v, want safe retry", err)
	}

	afterPost := &fakeReplyAPI{
		entries:   []generated.Entry{{Id: 202, Kind: "message"}},
		prefill:   &generated.MessageDraft{Addressed: generated.Addressed{Directly: []generated.Contact{{EmailAddress: "to@example.com"}}}},
		senderID:  9,
		createErr: errors.New("synthetic ambiguous mutation failure"),
	}
	err = runReply(context.Background(), afterPost, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{})
	if isSafeRetry(err) || len(afterPost.creates) != 1 {
		t.Fatalf("post-mutation error = %v, creates = %d; must make one attempt and not be retryable", err, len(afterPost.creates))
	}
}

func TestReplyDoesNotRetryPermanentPreMutationErrors(t *testing.T) {
	for name, permanent := range map[string]error{
		"authentication": hey.ErrAuth("not authenticated"),
		"forbidden":      hey.ErrForbidden("denied"),
		"not found":      &hey.Error{Code: hey.CodeNotFound, HTTPStatus: 404, Message: "missing"},
		"validation":     hey.ErrValidation("invalid"),
	} {
		t.Run(name, func(t *testing.T) {
			api := &fakeReplyAPI{entriesErr: permanent}
			err := runReply(context.Background(), api, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{})
			if isSafeRetry(err) {
				t.Fatalf("permanent pre-mutation error = %v, must not be retryable", err)
			}
		})
	}
}

func TestReplyClassifiesDefinitiveCreateRejectionRetryable(t *testing.T) {
	api := &fakeReplyAPI{
		entries:   []generated.Entry{{Id: 202, Kind: "message"}},
		prefill:   &generated.MessageDraft{Addressed: generated.Addressed{Directly: []generated.Contact{{EmailAddress: "to@example.com"}}}},
		senderID:  9,
		createErr: hey.ErrValidation("not ready"),
	}
	err := runReply(context.Background(), api, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{})
	if !isSafeRetry(err) {
		t.Fatalf("definitive create rejection = %v, want safe retry", err)
	}
}

func TestReplyRejectsMalformedOrEmptyInputBeforeNetwork(t *testing.T) {
	for _, input := range []string{`{`, `{}`, `{"content":""}`, `{"content":"ok","extra":true}`} {
		api := &fakeReplyAPI{}
		if err := runReply(context.Background(), api, 77, strings.NewReader(input), &bytes.Buffer{}); err == nil {
			t.Fatalf("input %q error = nil", input)
		}
		if len(api.newReplyIDs) != 0 || len(api.creates) != 0 {
			t.Fatalf("input %q made network calls", input)
		}
	}
}

func TestReplyRejectsCommentOnlyThread(t *testing.T) {
	api := &fakeReplyAPI{entries: []generated.Entry{{Id: 303, Kind: "comment"}}}
	if err := runReply(context.Background(), api, 77, strings.NewReader(`{"content":"safe"}`), &bytes.Buffer{}); err == nil {
		t.Fatal("comment-only thread error = nil")
	}
	if len(api.newReplyIDs) != 0 {
		t.Fatalf("NewReply called for comment: %v", api.newReplyIDs)
	}
}
