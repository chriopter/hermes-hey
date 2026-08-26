package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type globalWatchAPI struct {
	boxes       []generated.Box
	changes     map[int64]*hey.PostingChanges
	entries     map[int64][]generated.Entry
	messages    map[int64]*generated.Message
	changeCalls map[int64]int
}

func (f *globalWatchAPI) ListBoxes(context.Context) ([]generated.Box, error) { return f.boxes, nil }
func (f *globalWatchAPI) AllChanges(_ context.Context, boxID int64, _ hey.PostingChangesCursor) (*hey.PostingChanges, error) {
	f.changeCalls[boxID]++
	return f.changes[boxID], nil
}
func (f *globalWatchAPI) TopicEntries(_ context.Context, threadID int64) ([]generated.Entry, error) {
	return f.entries[threadID], nil
}
func (f *globalWatchAPI) Message(_ context.Context, entryID int64) (*generated.Message, error) {
	return f.messages[entryID], nil
}

func TestWatchOrdersCompleteBurstGloballyAndDeduplicates(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:00Z", Version: "1"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old, "22": old}}); err != nil {
		t.Fatal(err)
	}

	posting := func(postingID, threadID int64) generated.Posting {
		return generated.Posting{Id: postingID, AccountId: 7, Kind: "topic", AppUrl: "https://app.hey.com/topics/" + strconv.FormatInt(threadID, 10), Name: "Synthetic", VisibleEntryCount: 1}
	}
	message := func(entryID int64, created time.Time) *generated.Message {
		return &generated.Message{Id: entryID, Content: "synthetic", CreatedAt: created, Creator: generated.Contact{EmailAddress: "sender@example.com"}}
	}
	next11 := hey.PostingChangesCursor{Since: "2026-01-02T03:08:00Z", Version: "1"}
	next22 := hey.PostingChangesCursor{Since: "2026-01-02T03:09:00Z", Version: "1"}
	api := &globalWatchAPI{
		boxes: []generated.Box{{Id: 22, Kind: "trail"}, {Id: 11, Kind: "imbox"}},
		changes: map[int64]*hey.PostingChanges{
			11: {Added: []generated.Posting{posting(103, 33)}, Updated: []generated.Posting{posting(101, 31), posting(102, 32)}, NextCursor: &next11},
			22: {Added: []generated.Posting{posting(102, 32)}, NextCursor: &next22},
		},
		entries: map[int64][]generated.Entry{
			31: {{Id: 410, Kind: "message"}},
			32: {{Id: 420, Kind: "message"}},
			33: {{Id: 430, Kind: "message"}},
		},
		messages: map[int64]*generated.Message{
			410: message(410, time.Date(2026, 1, 2, 3, 5, 0, 0, time.UTC)),
			420: message(420, time.Date(2026, 1, 2, 3, 6, 0, 0, time.UTC)),
			430: message(430, time.Date(2026, 1, 2, 3, 5, 0, 0, time.UTC)),
		},
		changeCalls: make(map[int64]int),
	}
	acks := "{\"ack\":\"thread:31:entry:410\"}\n{\"ack\":\"thread:33:entry:430\"}\n{\"ack\":\"thread:32:entry:420\"}\n"
	var out bytes.Buffer
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", in: strings.NewReader(acks), out: &out}

	if err := engine.poll(context.Background()); err != nil {
		t.Fatal(err)
	}

	var got []string
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var frame watchEnvelope
		if err := json.Unmarshal([]byte(line), &frame); err != nil {
			t.Fatal(err)
		}
		got = append(got, frame.Event.EventID)
	}
	want := []string{"thread:31:entry:410", "thread:33:entry:430", "thread:32:entry:420"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("event order = %v, want %v", got, want)
	}
	if api.changeCalls[11] != 1 || api.changeCalls[22] != 1 {
		t.Fatalf("AllChanges calls = %v, want one bounded increment per box", api.changeCalls)
	}
	state, _, err := loadCursorState(path)
	if err != nil {
		t.Fatal(err)
	}
	if state.Boxes["11"].Since != next11.Since || state.Boxes["22"].Since != next22.Since {
		t.Fatalf("cursors = %#v, want both next cursors", state.Boxes)
	}
}
