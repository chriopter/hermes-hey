package main

import (
	"context"
	"testing"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
)

func TestVisibleMessageEntryIgnoresHistoricalComment(t *testing.T) {
	entriesNewestFirst := []generated.Entry{
		{Id: 303, Kind: "message"},
		{Id: 202, Kind: "comment"},
		{Id: 101, Kind: "message"},
	}

	entry := visibleMessageEntry(entriesNewestFirst, 3)
	if entry == nil || entry.Id != 303 {
		t.Fatalf("visibleMessageEntry() = %#v, want newest message 303", entry)
	}
}

func TestVisibleMessageEntryDoesNotHydrateComment(t *testing.T) {
	entriesNewestFirst := []generated.Entry{
		{Id: 303, Kind: "message"},
		{Id: 202, Kind: "comment"},
		{Id: 101, Kind: "message"},
	}

	if entry := visibleMessageEntry(entriesNewestFirst, 2); entry != nil {
		t.Fatalf("visibleMessageEntry() = %#v, want nil for comment", entry)
	}
}

func TestVisibleMessageEntryRejectsInvalidPosition(t *testing.T) {
	entries := []generated.Entry{{Id: 101, Kind: "message"}}
	for _, visibleCount := range []int{0, 2} {
		if entry := visibleMessageEntry(entries, visibleCount); entry != nil {
			t.Fatalf("visibleMessageEntry(%d) = %#v, want nil", visibleCount, entry)
		}
	}
}

type fakeHydrationAPI struct {
	entries      []generated.Entry
	message      *generated.Message
	messages     map[int64]*generated.Message
	messageCalls []int64
}

func (f *fakeHydrationAPI) TopicEntries(context.Context, int64) ([]generated.Entry, error) {
	return f.entries, nil
}

func (f *fakeHydrationAPI) Message(_ context.Context, id int64) (*generated.Message, error) {
	f.messageCalls = append(f.messageCalls, id)
	if f.messages != nil {
		return f.messages[id], nil
	}
	return f.message, nil
}

func TestHydratePostingSkipsHistoricalComment(t *testing.T) {
	createdAt := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	api := &fakeHydrationAPI{
		entries: []generated.Entry{
			{Id: 303, Kind: "message"},
			{Id: 202, Kind: "comment"},
			{Id: 101, Kind: "message"},
		},
		message: &generated.Message{
			Id:        303,
			Content:   "<p>Hello from the SDK</p>",
			CreatedAt: createdAt,
			Creator: generated.Contact{
				Id:           404,
				Name:         "Example Sender",
				EmailAddress: "sender@example.com",
			},
		},
	}
	posting := generated.Posting{
		Id:                505,
		Kind:              "topic",
		AppUrl:            "https://app.hey.com/topics/606",
		AccountId:         707,
		Name:              "Synthetic subject",
		VisibleEntryCount: 3,
	}

	event, err := hydratePosting(context.Background(), api, posting, "imbox")
	if err != nil {
		t.Fatal(err)
	}
	if event == nil || event.ThreadID != 606 || event.EntryID != 303 {
		t.Fatalf("hydratePosting() = %#v, want thread 606 entry 303", event)
	}
	if event.SenderEmail != "sender@example.com" || event.Content != "<p>Hello from the SDK</p>" {
		t.Fatalf("hydratePosting() = %#v, want synthetic sender and content", event)
	}
	if len(api.messageCalls) != 1 || api.messageCalls[0] != 303 {
		t.Fatalf("Message calls = %v, want only active message 303", api.messageCalls)
	}
}

func TestHydrationRejectsMismatchedSDKMessageID(t *testing.T) {
	posting := generated.Posting{
		Id: 505, Kind: "topic", AppUrl: "https://app.hey.com/topics/606",
		AccountId: 707, Name: "Synthetic subject", VisibleEntryCount: 1,
	}
	api := &fakeHydrationAPI{
		entries: []generated.Entry{{Id: 303, Kind: "message"}},
		message: &generated.Message{
			Id: 999, CreatedAt: time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC),
		},
	}

	t.Run("active posting", func(t *testing.T) {
		if event, err := hydratePosting(context.Background(), api, posting, "imbox"); err == nil || event != nil {
			t.Fatalf("hydratePosting() = (%#v, %v), want mismatched message ID error", event, err)
		}
	})
	t.Run("posting changes burst", func(t *testing.T) {
		events, err := hydratePostingSince(
			context.Background(), api, posting, "imbox", time.Date(2026, 1, 2, 3, 4, 0, 0, time.UTC),
		)
		if err == nil || events != nil {
			t.Fatalf("hydratePostingSince() = (%#v, %v), want mismatched message ID error", events, err)
		}
	})
}

func TestHydratePostingSinceReturnsEveryNewMessageChronologically(t *testing.T) {
	since := time.Date(2026, 1, 2, 3, 4, 0, 0, time.UTC)
	message := func(id int64, created time.Time) *generated.Message {
		return &generated.Message{
			Id: id, CreatedAt: created,
			Creator: generated.Contact{Name: "Example Sender", EmailAddress: "sender@example.com"},
		}
	}
	api := &fakeHydrationAPI{
		entries: []generated.Entry{
			{Id: 303, Kind: "message"},
			{Id: 250, Kind: "comment"},
			{Id: 202, Kind: "message"},
			{Id: 101, Kind: "message"},
		},
		messages: map[int64]*generated.Message{
			303: message(303, since.Add(2*time.Minute)),
			202: message(202, since.Add(time.Minute)),
			101: message(101, since.Add(-time.Minute)),
		},
	}
	posting := generated.Posting{
		Id: 505, Kind: "topic", AppUrl: "https://app.hey.com/topics/606",
		AccountId: 707, Name: "Synthetic subject", VisibleEntryCount: 4,
	}

	events, err := hydratePostingSince(context.Background(), api, posting, "imbox", since)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 2 || events[0].EntryID != 202 || events[1].EntryID != 303 {
		t.Fatalf("hydratePostingSince() = %#v, want entries 202 then 303", events)
	}
	if got := api.messageCalls; len(got) != 3 || got[0] != 303 || got[1] != 202 || got[2] != 101 {
		t.Fatalf("Message calls = %v, want [303 202 101] and never comment 250", got)
	}
}

func TestHydratePostingDoesNotLoadActiveComment(t *testing.T) {
	api := &fakeHydrationAPI{entries: []generated.Entry{
		{Id: 303, Kind: "message"},
		{Id: 202, Kind: "comment"},
		{Id: 101, Kind: "message"},
	}}
	posting := generated.Posting{
		Id:                505,
		Kind:              "topic",
		AppUrl:            "https://app.hey.com/topics/606",
		VisibleEntryCount: 2,
	}

	event, err := hydratePosting(context.Background(), api, posting, "imbox")
	if err != nil {
		t.Fatal(err)
	}
	if event != nil {
		t.Fatalf("hydratePosting() = %#v, want nil", event)
	}
	if len(api.messageCalls) != 0 {
		t.Fatalf("Message calls = %v, want none for active comment", api.messageCalls)
	}
}
