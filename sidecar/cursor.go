package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"

	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type cursorState struct {
	Version int                                 `json:"version"`
	Boxes   map[string]hey.PostingChangesCursor `json:"boxes"`
}

func newCursorState() cursorState {
	return cursorState{Version: 1, Boxes: make(map[string]hey.PostingChangesCursor)}
}

func loadCursorState(path string) (cursorState, bool, error) {
	state := newCursorState()
	pathInfo, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return state, false, nil
	}
	if err != nil {
		return state, false, fmt.Errorf("inspect cursor state: %w", err)
	}
	if !secureCursorFile(path, pathInfo) {
		return state, true, fmt.Errorf("cursor state is insecure")
	}
	if err := validateSecureCredentialDirectory(filepath.Dir(path)); err != nil {
		return state, true, fmt.Errorf("cursor directory is insecure: %w", err)
	}
	file, err := os.Open(path)
	if err != nil {
		return state, false, fmt.Errorf("open cursor state: %w", err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return state, true, fmt.Errorf("inspect cursor state: %w", err)
	}
	if info.Size() > maxStateBytes {
		return state, true, fmt.Errorf("invalid cursor state")
	}
	decoder := json.NewDecoder(io.LimitReader(file, maxStateBytes+1))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&state); err != nil {
		return newCursorState(), true, fmt.Errorf("invalid cursor state")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return newCursorState(), true, fmt.Errorf("invalid cursor state")
	}
	if state.Version != 1 || state.Boxes == nil {
		return newCursorState(), true, fmt.Errorf("invalid cursor state")
	}
	for id, cursor := range state.Boxes {
		parsed, err := strconv.ParseInt(id, 10, 64)
		if err != nil || parsed <= 0 || cursor.Since == "" {
			return newCursorState(), true, fmt.Errorf("invalid cursor state")
		}
	}
	return state, true, nil
}

func saveCursorState(path string, state cursorState) error {
	if path == "" || state.Version != 1 || state.Boxes == nil {
		return fmt.Errorf("invalid cursor state")
	}
	data, err := json.Marshal(state)
	if err != nil {
		return fmt.Errorf("encode cursor state: %w", err)
	}
	if len(data) > maxStateBytes {
		return fmt.Errorf("cursor state too large")
	}
	dir := filepath.Dir(path)
	if err := ensureSecureCredentialDirectory(dir); err != nil {
		return fmt.Errorf("create cursor directory: %w", err)
	}
	tmp, err := os.CreateTemp(dir, ".cursor-*.tmp")
	if err != nil {
		return fmt.Errorf("create cursor temp file: %w", err)
	}
	tmpPath := tmp.Name()
	ok := false
	defer func() {
		_ = tmp.Close()
		if !ok {
			_ = os.Remove(tmpPath)
		}
	}()
	if err := tmp.Chmod(0o600); err != nil {
		return fmt.Errorf("secure cursor temp file: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		return fmt.Errorf("write cursor state: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return fmt.Errorf("sync cursor state: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close cursor state: %w", err)
	}
	if err := replaceFile(tmpPath, path); err != nil {
		return fmt.Errorf("replace cursor state: %w", err)
	}
	ok = true
	if err := syncParentDirectory(dir); err != nil {
		return fmt.Errorf("sync cursor directory: %w", err)
	}
	return nil
}

const maxStateBytes = 1 << 20
