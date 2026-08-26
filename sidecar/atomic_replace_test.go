package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReplaceFileReplacesExistingDestination(t *testing.T) {
	dir := secureTempDir(t)
	destination := filepath.Join(dir, "state.json")
	source := filepath.Join(dir, "state.tmp")
	if err := os.WriteFile(destination, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(source, []byte("new"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := replaceFile(source, destination); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "new" {
		t.Fatalf("destination = %q, want new", got)
	}
	if _, err := os.Stat(source); !os.IsNotExist(err) {
		t.Fatalf("source still exists: %v", err)
	}
}
