package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type burstWatchAPI struct {
	boxes      []generated.Box
	changes    *hey.PostingChanges
	entries    []generated.Entry
	messages   map[int64]*generated.Message
	messageIDs []int64
}

func (f *burstWatchAPI) ListBoxes(context.Context) ([]generated.Box, error) {
	return f.boxes, nil
}

func (f *burstWatchAPI) AllChanges(context.Context, int64, hey.PostingChangesCursor) (*hey.PostingChanges, error) {
	return f.changes, nil
}

func (f *burstWatchAPI) TopicEntries(context.Context, int64) ([]generated.Entry, error) {
	return f.entries, nil
}

func (f *burstWatchAPI) Message(_ context.Context, id int64) (*generated.Message, error) {
	f.messageIDs = append(f.messageIDs, id)
	return f.messages[id], nil
}

func TestWatchEmitsMessagesAndCommentsFromSameThreadBurstChronologically(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:00.000Z", Version: "1"}
	next := hey.PostingChangesCursor{Since: "2026-01-02T03:07:00.000Z", Version: "1"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old}}); err != nil {
		t.Fatal(err)
	}
	message := func(id int64, minute int) *generated.Message {
		return &generated.Message{
			Id: id, Content: "synthetic",
			CreatedAt: time.Date(2026, 1, 2, 3, minute, 0, 0, time.UTC),
			Creator:   generated.Contact{Name: "Sender", EmailAddress: "sender@example.com"},
		}
	}
	posting := testPosting()
	posting.VisibleEntryCount = 5
	api := &burstWatchAPI{
		boxes:   []generated.Box{{Id: 11, Kind: "imbox"}},
		changes: &hey.PostingChanges{Updated: []generated.Posting{posting}, NextCursor: &next},
		entries: []generated.Entry{
			{
				Id: 44, Kind: "comment", Summary: "Agent response",
				CreatedAt: time.Date(2026, 1, 2, 3, 6, 30, 0, time.UTC),
				Creator:   generated.Contact{Id: 99, Name: "Agent", EmailAddress: "me@example.com"},
			},
			{Id: 43, Kind: "message"},
			{
				Id: 42, Kind: "comment", Summary: "Internal assignment",
				CreatedAt: time.Date(2026, 1, 2, 3, 5, 30, 0, time.UTC),
				Creator:   generated.Contact{Id: 88, Name: "Collaborator", EmailAddress: "collaborator@example.com"},
			},
			{Id: 41, Kind: "message"},
			{Id: 40, Kind: "message"},
		},
		messages: map[int64]*generated.Message{
			43: message(43, 6),
			41: message(41, 5),
			40: message(40, 3),
		},
	}
	var out bytes.Buffer
	acks := "{\"ack\":\"thread:31:entry:41\"}\n{\"ack\":\"thread:31:entry:42\"}\n{\"ack\":\"thread:31:entry:43\"}\n"
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &out, in: strings.NewReader(acks)}

	if err := engine.poll(context.Background()); err != nil {
		t.Fatal(err)
	}

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("event lines = %d, want 3: %q", len(lines), out.String())
	}
	var got []int64
	var kinds []string
	for _, line := range lines {
		var frame struct {
			Event event `json:"event"`
		}
		if err := json.Unmarshal([]byte(line), &frame); err != nil {
			t.Fatal(err)
		}
		got = append(got, frame.Event.EntryID)
		kinds = append(kinds, frame.Event.Kind)
	}
	if got[0] != 41 || got[1] != 42 || got[2] != 43 {
		t.Fatalf("event entry order = %v, want [41 42 43]", got)
	}
	if strings.Join(kinds, ",") != "message,comment,message" {
		t.Fatalf("event kinds = %v", kinds)
	}
	if len(api.messageIDs) != 3 || api.messageIDs[0] != 43 || api.messageIDs[1] != 41 || api.messageIDs[2] != 40 {
		t.Fatalf("Message calls = %v, want [43 41 40] and never comment 42", api.messageIDs)
	}
	state, _, err := loadCursorState(path)
	if err != nil {
		t.Fatal(err)
	}
	if state.Boxes["11"].Since != next.Since {
		t.Fatal("cursor did not advance after both event acknowledgements")
	}
}
