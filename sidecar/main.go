package main

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
)

type hydrationAPI interface {
	TopicEntries(context.Context, int64) ([]generated.Entry, error)
	Message(context.Context, int64) (*generated.Message, error)
}

type event struct {
	EventID     string `json:"event_id"`
	PostingID   int64  `json:"posting_id"`
	ThreadID    int64  `json:"thread_id"`
	EntryID     int64  `json:"entry_id"`
	AccountID   int64  `json:"account_id,omitempty"`
	SenderID    int64  `json:"sender_id,omitempty"`
	SenderName  string `json:"sender_name"`
	SenderEmail string `json:"sender_email"`
	Subject     string `json:"subject"`
	Content     string `json:"content"`
	AppURL      string `json:"app_url"`
	CreatedAt   string `json:"created_at"`
	BoxKind     string `json:"box_kind"`
}

func visibleMessageEntry(entriesNewestFirst []generated.Entry, visibleCount int) *generated.Entry {
	if visibleCount < 1 || visibleCount > len(entriesNewestFirst) {
		return nil
	}
	index := len(entriesNewestFirst) - visibleCount
	entry := &entriesNewestFirst[index]
	if entry.Kind != "message" {
		return nil
	}
	return entry
}

func topicIDFromURL(value string) (int64, error) {
	parsed, err := url.Parse(value)
	if err != nil {
		return 0, fmt.Errorf("invalid topic URL")
	}
	parts := strings.Split(strings.Trim(parsed.Path, "/"), "/")
	if len(parts) != 2 || parts[0] != "topics" {
		return 0, fmt.Errorf("invalid topic URL")
	}
	id, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || id <= 0 {
		return 0, fmt.Errorf("invalid topic URL")
	}
	return id, nil
}

func hydratePosting(ctx context.Context, api hydrationAPI, posting generated.Posting, boxKind string) (*event, error) {
	threadID, err := topicIDFromURL(posting.AppUrl)
	if err != nil {
		return nil, err
	}
	entries, err := api.TopicEntries(ctx, threadID)
	if err != nil {
		return nil, err
	}
	entry := visibleMessageEntry(entries, int(posting.VisibleEntryCount))
	if entry == nil {
		return nil, nil
	}
	message, err := api.Message(ctx, entry.Id)
	if err != nil {
		return nil, err
	}
	if err := validateHydratedMessage(entry.Id, message); err != nil {
		return nil, err
	}
	return eventFromMessage(posting, threadID, entry.Id, message, boxKind), nil
}

func hydratePostingSince(ctx context.Context, api hydrationAPI, posting generated.Posting, boxKind string, since time.Time) ([]*event, error) {
	threadID, err := topicIDFromURL(posting.AppUrl)
	if err != nil {
		return nil, err
	}
	entries, err := api.TopicEntries(ctx, threadID)
	if err != nil {
		return nil, err
	}
	active := visibleMessageEntry(entries, int(posting.VisibleEntryCount))
	var activeID int64
	if active != nil {
		activeID = active.Id
	}
	events := make([]*event, 0)
	for _, entry := range entries {
		if entry.Kind != "message" {
			continue
		}
		message, err := api.Message(ctx, entry.Id)
		if err != nil {
			return nil, err
		}
		if err := validateHydratedMessage(entry.Id, message); err != nil {
			return nil, err
		}
		isNew := message.CreatedAt.After(since)
		if !isNew && entry.Id != activeID {
			break
		}
		events = append(events, eventFromMessage(posting, threadID, entry.Id, message, boxKind))
		if !isNew {
			break
		}
	}
	for left, right := 0, len(events)-1; left < right; left, right = left+1, right-1 {
		events[left], events[right] = events[right], events[left]
	}
	return events, nil
}

func validateHydratedMessage(entryID int64, message *generated.Message) error {
	if message == nil {
		return fmt.Errorf("message returned no data")
	}
	if entryID <= 0 || message.Id <= 0 || message.Id != entryID {
		return fmt.Errorf("message ID does not match entry")
	}
	return nil
}

func eventFromMessage(posting generated.Posting, threadID, entryID int64, message *generated.Message, boxKind string) *event {
	return &event{
		EventID:     fmt.Sprintf("thread:%d:entry:%d", threadID, entryID),
		PostingID:   posting.Id,
		ThreadID:    threadID,
		EntryID:     entryID,
		AccountID:   posting.AccountId,
		SenderID:    message.Creator.Id,
		SenderName:  message.Creator.Name,
		SenderEmail: strings.ToLower(strings.TrimSpace(message.Creator.EmailAddress)),
		Subject:     posting.Name,
		Content:     message.Content,
		AppURL:      fmt.Sprintf("https://app.hey.com/topics/%d", threadID),
		CreatedAt:   message.CreatedAt.Format("2006-01-02T15:04:05.999999999Z07:00"),
		BoxKind:     boxKind,
	}
}
