package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type fakeWatchAPI struct {
	boxes       []generated.Box
	changes     map[int64]*hey.PostingChanges
	entries     []generated.Entry
	message     *generated.Message
	changeCalls int
	messageIDs  []int64
}

func (f *fakeWatchAPI) ListBoxes(context.Context) ([]generated.Box, error) { return f.boxes, nil }
func (f *fakeWatchAPI) AllChanges(_ context.Context, _ int64, _ hey.PostingChangesCursor) (*hey.PostingChanges, error) {
	f.changeCalls++
	for _, value := range f.changes {
		return value, nil
	}
	return &hey.PostingChanges{}, nil
}
func (f *fakeWatchAPI) TopicEntries(context.Context, int64) ([]generated.Entry, error) {
	return f.entries, nil
}
func (f *fakeWatchAPI) Message(_ context.Context, id int64) (*generated.Message, error) {
	f.messageIDs = append(f.messageIDs, id)
	return f.message, nil
}

func testPosting() generated.Posting {
	return generated.Posting{Id: 21, AccountId: 7, Kind: "topic", AppUrl: "https://app.hey.com/topics/31", Name: "Synthetic", VisibleEntryCount: 1}
}

func TestWaitForAckReturnsWhenContextIsCanceled(t *testing.T) {
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	defer writer.Close()

	ctx, cancel := context.WithCancel(context.Background())
	engine := watchEngine{in: reader}
	done := make(chan error, 1)
	go func() { done <- engine.waitForAck(ctx, "synthetic-event") }()
	time.Sleep(20 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("waitForAck() error = %v, want context canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("waitForAck() did not return after cancellation")
	}
}

func TestWatchFirstRunBaselinesWithoutHistory(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	api := &fakeWatchAPI{boxes: []generated.Box{{Id: 11, Kind: "imbox", PostingChangesUrl: "https://app.hey.com/boxes/11/posting_changes?since=2026-01-02T03%3A04%3A05.000Z&v=1"}}}
	var out bytes.Buffer
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &out, in: strings.NewReader("")}
	if err := engine.initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	if api.changeCalls != 0 {
		t.Fatalf("AllChanges calls = %d, want zero on baseline", api.changeCalls)
	}
	state, exists, err := loadCursorState(path)
	if err != nil || !exists || state.Boxes["11"].Since == "" {
		t.Fatalf("baseline state = %#v, %v, %v", state, exists, err)
	}
	var ready struct {
		Type            string `json:"type"`
		ProtocolVersion int    `json:"protocol_version"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(out.Bytes()), &ready); err != nil {
		t.Fatal(err)
	}
	if ready.Type != "ready" || ready.ProtocolVersion != 1 {
		t.Fatalf("ready output = %#v", ready)
	}
}

func TestWatchRejectsChangedIncrementWithoutNextCursorBeforeEmission(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:05.000Z", Version: "1"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old}}); err != nil {
		t.Fatal(err)
	}
	api := &fakeWatchAPI{
		boxes:   []generated.Box{{Id: 11, Kind: "imbox"}},
		changes: map[int64]*hey.PostingChanges{11: {Added: []generated.Posting{testPosting()}}},
		entries: []generated.Entry{{Id: 41, Kind: "message"}},
		message: &generated.Message{Id: 41, Creator: generated.Contact{EmailAddress: "sender@example.com"}},
	}
	var out bytes.Buffer
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &out, in: strings.NewReader("")}
	if err := engine.poll(context.Background()); err == nil {
		t.Fatal("changed increment without next cursor succeeded")
	}
	if out.Len() != 0 || len(api.messageIDs) != 0 {
		t.Fatalf("invalid increment emitted or hydrated data: output=%q messageIDs=%v", out.String(), api.messageIDs)
	}
}

func TestWatchPersistsCursorOnlyAfterAck(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:05.000Z", Version: "1"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old}}); err != nil {
		t.Fatal(err)
	}
	created := time.Date(2026, 1, 2, 3, 5, 0, 0, time.UTC)
	api := &fakeWatchAPI{
		boxes:   []generated.Box{{Id: 11, Kind: "imbox"}},
		changes: map[int64]*hey.PostingChanges{11: {Added: []generated.Posting{testPosting()}, NextCursor: &hey.PostingChangesCursor{Since: "2026-01-02T03:05:05.000Z", Version: "1"}}},
		entries: []generated.Entry{{Id: 41, Kind: "message"}},
		message: &generated.Message{Id: 41, Content: "safe content", CreatedAt: created, Creator: generated.Contact{Id: 51, Name: "Sender", EmailAddress: "sender@example.com"}},
	}
	var out bytes.Buffer
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &out, in: strings.NewReader("")}
	err := engine.poll(context.Background())
	if !errors.Is(err, errAckEOF) {
		t.Fatalf("poll error = %v, want ack EOF", err)
	}
	state, _, _ := loadCursorState(path)
	if state.Boxes["11"].Since != old.Since {
		t.Fatalf("cursor advanced before ack: %#v", state.Boxes["11"])
	}

	var line struct {
		Type  string `json:"type"`
		Event event  `json:"event"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(out.Bytes()), &line); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	engine.in = strings.NewReader(`{"ack":"` + line.Event.EventID + `"}` + "\n")
	if err := engine.poll(context.Background()); err != nil {
		t.Fatal(err)
	}
	state, _, _ = loadCursorState(path)
	if state.Boxes["11"].Since == old.Since {
		t.Fatal("cursor did not advance after ack")
	}
}

func TestWatchRestartBeforeAckReplaysEvent(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:05.000Z", Version: "1"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old}}); err != nil {
		t.Fatal(err)
	}
	next := hey.PostingChangesCursor{Since: "2026-01-02T03:05:05.000Z", Version: "1"}
	api := &fakeWatchAPI{boxes: []generated.Box{{Id: 11, Kind: "imbox"}}, changes: map[int64]*hey.PostingChanges{11: {Added: []generated.Posting{testPosting()}, NextCursor: &next}}, entries: []generated.Entry{{Id: 41, Kind: "message"}}, message: &generated.Message{Id: 41, Creator: generated.Contact{EmailAddress: "sender@example.com"}}}
	var first, second bytes.Buffer
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &first, in: strings.NewReader("")}
	_ = engine.poll(context.Background())
	engine.out, engine.in = &second, strings.NewReader("")
	_ = engine.poll(context.Background())
	if first.String() == "" || first.String() != second.String() {
		t.Fatalf("replay mismatch: first %q second %q", first.String(), second.String())
	}
}

func TestWatchConsumesOneAckPerEventWithoutLosingBufferedInput(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:05.000Z"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old}}); err != nil {
		t.Fatal(err)
	}
	second := testPosting()
	second.Id, second.AppUrl = 22, "https://app.hey.com/topics/32"
	next := hey.PostingChangesCursor{Since: "2026-01-02T03:05:05.000Z"}
	api := &fakeWatchAPI{boxes: []generated.Box{{Id: 11, Kind: "imbox"}}, changes: map[int64]*hey.PostingChanges{11: {Added: []generated.Posting{testPosting(), second}, NextCursor: &next}}, entries: []generated.Entry{{Id: 41, Kind: "message"}}, message: &generated.Message{Id: 41, Creator: generated.Contact{EmailAddress: "sender@example.com"}}}
	acks := "{\"ack\":\"thread:31:entry:41\"}\n{\"ack\":\"thread:32:entry:41\"}\n"
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &bytes.Buffer{}, in: strings.NewReader(acks)}
	if err := engine.poll(context.Background()); err != nil {
		t.Fatal(err)
	}
	state, _, _ := loadCursorState(path)
	if state.Boxes["11"].Since != next.Since {
		t.Fatal("cursor did not advance after both buffered acks")
	}
}

func TestWatchSuppressesOwnSenderAndAdvances(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	old := hey.PostingChangesCursor{Since: "2026-01-02T03:04:05.000Z"}
	if err := saveCursorState(path, cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{"11": old}}); err != nil {
		t.Fatal(err)
	}
	next := hey.PostingChangesCursor{Since: "2026-01-02T03:05:05.000Z"}
	api := &fakeWatchAPI{boxes: []generated.Box{{Id: 11, Kind: "imbox"}}, changes: map[int64]*hey.PostingChanges{11: {Updated: []generated.Posting{testPosting()}, NextCursor: &next}}, entries: []generated.Entry{{Id: 41, Kind: "message"}}, message: &generated.Message{Id: 41, Creator: generated.Contact{EmailAddress: " ME@EXAMPLE.COM "}}}
	var out bytes.Buffer
	engine := watchEngine{api: api, statePath: path, ownEmail: "me@example.com", out: &out, in: strings.NewReader("")}
	if err := engine.poll(context.Background()); err != nil {
		t.Fatal(err)
	}
	if out.Len() != 0 {
		t.Fatalf("own event output = %q, want none", out.String())
	}
	state, _, _ := loadCursorState(path)
	if state.Boxes["11"].Since != next.Since {
		t.Fatal("own event did not advance cursor")
	}
}
