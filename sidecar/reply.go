package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type replyAPI interface {
	TopicEntries(context.Context, int64) ([]generated.Entry, error)
	NewReply(context.Context, int64) (*generated.MessageDraft, error)
	DefaultSenderID(context.Context) (int64, error)
	CreateReply(context.Context, int64, string, []string, []string, []string) error
}

type safeRetryError struct {
	err error
}

func (e safeRetryError) Error() string { return e.err.Error() }
func (e safeRetryError) Unwrap() error { return e.err }

func safeRetry(err error) error {
	if err == nil {
		return nil
	}
	return safeRetryError{err: err}
}

func isSafeRetry(err error) bool {
	var target safeRetryError
	return errors.As(err, &target)
}

func runReply(ctx context.Context, api replyAPI, threadID int64, in io.Reader, out io.Writer) error {
	if threadID <= 0 {
		return fmt.Errorf("thread ID must be positive")
	}
	var input struct {
		Content string `json:"content"`
	}
	decoder := json.NewDecoder(io.LimitReader(in, maxReplyInputBytes+1))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		return fmt.Errorf("invalid reply input")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return fmt.Errorf("invalid reply input")
	}
	if strings.TrimSpace(input.Content) == "" || len(input.Content) > maxReplyBodyBytes {
		return fmt.Errorf("invalid reply content")
	}
	entries, err := api.TopicEntries(ctx, threadID)
	if err != nil {
		wrapped := fmt.Errorf("list thread entries: %w", err)
		if isTransientReadError(err) {
			return safeRetry(wrapped)
		}
		return wrapped
	}
	var entryID int64
	for _, entry := range entries {
		if entry.Kind == "message" && entry.Id > 0 {
			entryID = entry.Id
			break
		}
	}
	if entryID == 0 {
		return fmt.Errorf("thread has no replyable message")
	}
	prefill, err := api.NewReply(ctx, entryID)
	if err != nil {
		wrapped := fmt.Errorf("load reply recipients: %w", err)
		if isTransientReadError(err) {
			return safeRetry(wrapped)
		}
		return wrapped
	}
	if prefill == nil {
		return fmt.Errorf("reply recipients returned no data")
	}
	to := contactEmails(prefill.Addressed.Directly)
	cc := contactEmails(prefill.Addressed.Copied)
	bcc := contactEmails(prefill.Addressed.Blindcopied)
	if len(to)+len(cc)+len(bcc) == 0 {
		return fmt.Errorf("reply has no recipients")
	}
	senderID, err := api.DefaultSenderID(ctx)
	if err != nil {
		wrapped := fmt.Errorf("resolve reply sender: %w", err)
		if isTransientReadError(err) {
			return safeRetry(wrapped)
		}
		return wrapped
	}
	if senderID <= 0 {
		return fmt.Errorf("resolve reply sender: no default sender")
	}
	if err := api.CreateReply(ctx, entryID, input.Content, to, cc, bcc); err != nil {
		wrapped := fmt.Errorf("create reply: %w", err)
		if isDefinitiveCreateRejection(err) {
			return safeRetry(wrapped)
		}
		return wrapped
	}
	return writeNDJSON(out, struct {
		OK bool `json:"ok"`
	}{OK: true})
}

func isTransientReadError(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	if errors.Is(err, context.Canceled) {
		return false
	}
	var apiError *hey.Error
	if errors.As(err, &apiError) {
		if apiError.Code == hey.CodeNetwork || apiError.Code == hey.CodeRateLimit {
			return true
		}
		return apiError.HTTPStatus == 408 || apiError.HTTPStatus == 425 || apiError.HTTPStatus == 429 || apiError.HTTPStatus >= 500
	}
	var networkError net.Error
	return errors.As(err, &networkError) && (networkError.Timeout() || networkError.Temporary())
}

func isDefinitiveCreateRejection(err error) bool {
	var apiError *hey.Error
	if !errors.As(err, &apiError) {
		return false
	}
	switch apiError.HTTPStatus {
	case 404, 409, 422, 429:
		return true
	default:
		return false
	}
}

func contactEmails(contacts []generated.Contact) []string {
	result := make([]string, 0, len(contacts))
	seen := make(map[string]bool)
	for _, contact := range contacts {
		email := strings.TrimSpace(contact.EmailAddress)
		key := strings.ToLower(email)
		if email != "" && !seen[key] {
			seen[key] = true
			result = append(result, email)
		}
	}
	return result
}

const (
	maxReplyInputBytes = 1 << 20
	maxReplyBodyBytes  = 512 << 10
)
