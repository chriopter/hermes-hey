package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

func secureTempDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestCursorStateRejectsInsecureParentDirectory(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX permission check")
	}
	dir := secureTempDir(t)
	path := filepath.Join(dir, "cursor.json")
	if err := os.WriteFile(path, []byte(`{"version":1,"boxes":{}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadCursorState(path); err == nil {
		t.Fatal("loadCursorState() error = nil, want insecure parent error")
	}
}

func TestCursorStateRejectsSymlink(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX symlink check")
	}
	dir := secureTempDir(t)
	target := filepath.Join(secureTempDir(t), "target.json")
	if err := os.WriteFile(target, []byte(`{"version":1,"boxes":{}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "cursor.json")
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadCursorState(path); err == nil {
		t.Fatal("loadCursorState() error = nil, want symlink error")
	}
}

func TestCursorStateRejectsNonOwnerReadWriteMode(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX permission check")
	}
	path := filepath.Join(secureTempDir(t), "cursor.json")
	if err := os.WriteFile(path, []byte(`{"version":1,"boxes":{}}`), 0o400); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadCursorState(path); err == nil {
		t.Fatal("loadCursorState() error = nil, want non-0600 mode error")
	}
}

func TestCursorStateRoundTripIsPrivate(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	want := cursorState{Version: 1, Boxes: map[string]hey.PostingChangesCursor{
		"11": {Since: "2026-01-02T03:04:05.000Z", Version: "1"},
	}}
	if err := saveCursorState(path, want); err != nil {
		t.Fatal(err)
	}
	got, exists, err := loadCursorState(path)
	if err != nil || !exists || got.Boxes["11"].Since != want.Boxes["11"].Since {
		t.Fatalf("loadCursorState() = %#v, %v, %v", got, exists, err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("cursor mode = %o, want 600", info.Mode().Perm())
	}
}

func TestCursorStateCorruptionFailsClosed(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	if err := os.WriteFile(path, []byte(`{"version":1,"boxes":`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadCursorState(path); err == nil {
		t.Fatal("loadCursorState() error = nil, want corruption error")
	}
}

func TestOversizedCursorStateFailsClosed(t *testing.T) {
	path := filepath.Join(secureTempDir(t), "cursor.json")
	data := append([]byte(`{"version":1,"boxes":{}}`), bytes.Repeat([]byte(" "), maxStateBytes)...)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadCursorState(path); err == nil {
		t.Fatal("loadCursorState() error = nil, want size error")
	}
}

func TestMissingCursorStateIsNotAnError(t *testing.T) {
	got, exists, err := loadCursorState(filepath.Join(secureTempDir(t), "missing.json"))
	if err != nil || exists || got.Version != 1 || got.Boxes == nil {
		t.Fatalf("loadCursorState() = %#v, %v, %v", got, exists, err)
	}
}
