package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

type commentAPI interface {
	CreateComment(context.Context, int64, string) error
}

func runComment(ctx context.Context, api commentAPI, threadID int64, in io.Reader, out io.Writer) error {
	if threadID <= 0 {
		return fmt.Errorf("thread ID must be positive")
	}
	var input struct {
		Content string `json:"content"`
	}
	decoder := json.NewDecoder(io.LimitReader(in, maxReplyInputBytes+1))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		return fmt.Errorf("invalid comment input")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return fmt.Errorf("invalid comment input")
	}
	if strings.TrimSpace(input.Content) == "" || len(input.Content) > maxReplyBodyBytes {
		return fmt.Errorf("invalid comment content")
	}
	if err := api.CreateComment(ctx, threadID, input.Content); err != nil {
		wrapped := fmt.Errorf("create comment: %w", err)
		if isDefinitiveCreateRejection(err) {
			return safeRetry(wrapped)
		}
		return wrapped
	}
	return writeNDJSON(out, struct {
		OK bool `json:"ok"`
	}{OK: true})
}
