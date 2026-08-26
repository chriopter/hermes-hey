package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

var errAckEOF = errors.New("ack input closed")

type watchAPI interface {
	ListBoxes(context.Context) ([]generated.Box, error)
	AllChanges(context.Context, int64, hey.PostingChangesCursor) (*hey.PostingChanges, error)
	hydrationAPI
}

type watchEngine struct {
	api       watchAPI
	statePath string
	ownEmail  string
	in        io.Reader
	out       io.Writer
	ackReader *bufio.Reader
}

type watchEnvelope struct {
	Type            string `json:"type"`
	ProtocolVersion int    `json:"protocol_version,omitempty"`
	Event           *event `json:"event,omitempty"`
}

func (e *watchEngine) initialize(ctx context.Context) error {
	state, exists, err := loadCursorState(e.statePath)
	if err != nil {
		return err
	}
	boxes, err := e.api.ListBoxes(ctx)
	if err != nil {
		return fmt.Errorf("list boxes: %w", err)
	}
	changed := !exists
	for _, box := range boxes {
		if box.Id <= 0 || box.PostingChangesUrl == "" {
			return fmt.Errorf("box has invalid changes cursor")
		}
		key := strconv.FormatInt(box.Id, 10)
		if _, ok := state.Boxes[key]; ok {
			continue
		}
		cursor, err := hey.PostingChangesCursorFrom(box.PostingChangesUrl)
		if err != nil || cursor.Since == "" {
			return fmt.Errorf("box has invalid changes cursor")
		}
		state.Boxes[key] = cursor
		changed = true
	}
	if changed {
		if err := saveCursorState(e.statePath, state); err != nil {
			return err
		}
	}
	return writeNDJSON(e.out, watchEnvelope{Type: "ready", ProtocolVersion: protocolVersion})
}

func (e *watchEngine) poll(ctx context.Context) error {
	state, exists, err := loadCursorState(e.statePath)
	if err != nil {
		return err
	}
	if !exists {
		return fmt.Errorf("cursor state is not initialized")
	}
	boxes, err := e.api.ListBoxes(ctx)
	if err != nil {
		return fmt.Errorf("list boxes: %w", err)
	}
	sort.Slice(boxes, func(i, j int) bool { return boxes[i].Id < boxes[j].Id })

	type increment struct {
		key      string
		boxKind  string
		since    time.Time
		postings []generated.Posting
		next     *hey.PostingChangesCursor
		pending  int
	}
	increments := make([]*increment, 0, len(boxes))
	byKey := make(map[string]*increment, len(boxes))
	for _, box := range boxes {
		key := strconv.FormatInt(box.Id, 10)
		cursor, ok := state.Boxes[key]
		if !ok {
			return fmt.Errorf("cursor state missing box")
		}
		since, err := time.Parse(time.RFC3339Nano, cursor.Since)
		if err != nil {
			return fmt.Errorf("cursor state has invalid timestamp")
		}
		changes, err := e.api.AllChanges(ctx, box.Id, cursor)
		if err != nil {
			return fmt.Errorf("read posting changes: %w", err)
		}
		if changes == nil {
			return fmt.Errorf("posting changes returned no data")
		}
		if changes.FullSyncRequired {
			return fmt.Errorf("posting changes require a full sync")
		}
		changed := len(changes.Added)+len(changes.Updated)+len(changes.Deleted) > 0
		if changed && changes.NextCursor == nil {
			return fmt.Errorf("posting changes missing next cursor")
		}
		if changes.NextCursor != nil && changes.NextCursor.Since == "" {
			return fmt.Errorf("posting changes returned invalid cursor")
		}
		postings := append(append([]generated.Posting(nil), changes.Added...), changes.Updated...)
		sort.SliceStable(postings, func(i, j int) bool {
			if postings[i].Id != postings[j].Id {
				return postings[i].Id < postings[j].Id
			}
			return postings[i].AppUrl < postings[j].AppUrl
		})
		inc := &increment{key: key, boxKind: box.Kind, since: since, postings: postings, next: changes.NextCursor}
		increments = append(increments, inc)
		byKey[key] = inc
	}

	type pendingEvent struct {
		event   *event
		created time.Time
		boxes   map[string]bool
	}
	pendingByID := make(map[string]*pendingEvent)
	for _, inc := range increments {
		for _, posting := range inc.postings {
			if posting.Id <= 0 || posting.Kind != "topic" {
				continue
			}
			events, err := hydratePostingSince(ctx, e.api, posting, inc.boxKind, inc.since)
			if err != nil {
				return fmt.Errorf("hydrate posting: %w", err)
			}
			for _, ev := range events {
				if strings.EqualFold(strings.TrimSpace(ev.SenderEmail), strings.TrimSpace(e.ownEmail)) {
					continue
				}
				item := pendingByID[ev.EventID]
				if item == nil {
					created, err := time.Parse(time.RFC3339Nano, ev.CreatedAt)
					if err != nil {
						return fmt.Errorf("hydrated event has invalid timestamp")
					}
					item = &pendingEvent{event: ev, created: created, boxes: make(map[string]bool)}
					pendingByID[ev.EventID] = item
				}
				item.boxes[inc.key] = true
			}
		}
	}

	pending := make([]*pendingEvent, 0, len(pendingByID))
	for _, item := range pendingByID {
		pending = append(pending, item)
		for key := range item.boxes {
			byKey[key].pending++
		}
	}
	sort.Slice(pending, func(i, j int) bool {
		if !pending[i].created.Equal(pending[j].created) {
			return pending[i].created.Before(pending[j].created)
		}
		if pending[i].event.ThreadID != pending[j].event.ThreadID {
			return pending[i].event.ThreadID < pending[j].event.ThreadID
		}
		if pending[i].event.EntryID != pending[j].event.EntryID {
			return pending[i].event.EntryID < pending[j].event.EntryID
		}
		return pending[i].event.EventID < pending[j].event.EventID
	})

	stateChanged := false
	for _, inc := range increments {
		if inc.next != nil && inc.pending == 0 {
			state.Boxes[inc.key] = *inc.next
			stateChanged = true
		}
	}
	if stateChanged {
		if err := saveCursorState(e.statePath, state); err != nil {
			return err
		}
	}
	for _, item := range pending {
		if err := writeNDJSON(e.out, watchEnvelope{Type: "event", Event: item.event}); err != nil {
			return err
		}
		if err := e.waitForAck(ctx, item.event.EventID); err != nil {
			e.ackReader = nil
			return err
		}
		stateChanged = false
		for key := range item.boxes {
			inc := byKey[key]
			inc.pending--
			if inc.pending == 0 && inc.next != nil {
				state.Boxes[key] = *inc.next
				stateChanged = true
			}
		}
		if stateChanged {
			if err := saveCursorState(e.statePath, state); err != nil {
				return err
			}
		}
	}
	return nil
}

func writeNDJSON(w io.Writer, value any) error {
	if w == nil {
		return fmt.Errorf("output is unavailable")
	}
	data, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("encode output: %w", err)
	}
	if len(data) > maxNDJSONBytes {
		return fmt.Errorf("output record too large")
	}
	_, err = fmt.Fprintf(w, "%s\n", data)
	return err
}

func (e *watchEngine) waitForAck(ctx context.Context, eventID string) error {
	if e.in == nil {
		return errAckEOF
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if e.ackReader == nil {
		e.ackReader = bufio.NewReader(e.in)
	}
	var stopDeadlineWatch func()
	// Deadline-based cancellation is an optimisation, not a requirement. When
	// the gateway spawns us, stdin is a *blocking* pipe: Go cannot register it
	// with the runtime poller, so SetReadDeadline reports ErrNoDeadline. Hard
	// failing there aborted every watch right after the first event, which the
	// host could only see as a fatal frame. Fall back to a plain blocking read
	// instead — the parent terminates our process group on shutdown.
	var deadliner readDeadliner
	if ctx.Done() != nil {
		var err error
		deadliner, err = ackReadDeadliner(e.in)
		if err != nil {
			return err
		}
	}
	if deadliner != nil {
		done := make(chan struct{})
		watcherDone := make(chan struct{})
		go func() {
			defer close(watcherDone)
			select {
			case <-ctx.Done():
				_ = deadliner.SetReadDeadline(time.Now())
			case <-done:
			}
		}()
		stopDeadlineWatch = func() {
			close(done)
			<-watcherDone
			_ = deadliner.SetReadDeadline(time.Time{})
		}
		defer stopDeadlineWatch()
	}
	line, err := readLimitedLine(e.ackReader, maxNDJSONBytes)
	if ctxErr := ctx.Err(); ctxErr != nil {
		return ctxErr
	}
	if errors.Is(err, io.EOF) && len(line) == 0 {
		return errAckEOF
	}
	if err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("read ack: %w", err)
	}
	var ack struct {
		Ack string `json:"ack"`
	}
	decoder := json.NewDecoder(strings.NewReader(string(line)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&ack); err != nil || ack.Ack != eventID {
		return fmt.Errorf("invalid ack")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return fmt.Errorf("invalid ack")
	}
	return nil
}

type readDeadliner interface {
	SetReadDeadline(time.Time) error
}

// ackReadDeadliner returns deadline support when it is usable. Inputs without
// deadline support (a blocking pipe, a plain file) are read normally.
func ackReadDeadliner(in io.Reader) (readDeadliner, error) {
	deadliner, ok := in.(readDeadliner)
	if !ok {
		return nil, nil
	}
	if err := deadliner.SetReadDeadline(time.Time{}); err != nil {
		if errors.Is(err, os.ErrNoDeadline) {
			return nil, nil
		}
		return nil, fmt.Errorf("acknowledgement deadline is unavailable")
	}
	return deadliner, nil
}

func readLimitedLine(reader *bufio.Reader, limit int) ([]byte, error) {
	var line []byte
	for {
		part, err := reader.ReadSlice('\n')
		if len(line)+len(part) > limit {
			return nil, fmt.Errorf("ack record too large")
		}
		line = append(line, part...)
		if err == nil || !errors.Is(err, bufio.ErrBufferFull) {
			return line, err
		}
	}
}

const maxNDJSONBytes = 4 << 20
