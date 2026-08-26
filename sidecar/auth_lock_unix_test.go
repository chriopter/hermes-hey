//go:build unix

package main

import (
	"context"
	"os"
	"testing"
)

func TestCredentialManagerRejectsCredentialSymlink(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	targetDir := t.TempDir()
	writeCredentialFixture(t, targetDir, "https://example.test", storedCredentialFixture{AccessToken: "synthetic-access"})
	if err := os.Symlink(targetDir+"/credentials.json", dir+"/credentials.json"); err != nil {
		t.Fatal(err)
	}
	manager := newCredentialManager(dir, "https://example.test", nil)
	if _, err := manager.AccessToken(context.Background()); err == nil {
		t.Fatal("AccessToken() error = nil, want symlink error")
	}
}

func TestCredentialManagerRejectsCredentialFileOwnedByAnotherUser(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing fixture ownership requires root")
	}
	dir := t.TempDir()
	writeCredentialFixture(t, dir, "https://example.test", storedCredentialFixture{AccessToken: "synthetic-access"})
	if err := os.Chown(dir+"/credentials.json", 1, -1); err != nil {
		t.Fatal(err)
	}
	manager := newCredentialManager(dir, "https://example.test", nil)
	if _, err := manager.AccessToken(context.Background()); err == nil {
		t.Fatal("AccessToken() error = nil, want credential file owner error")
	}
}

func TestCursorStateRejectsFileOwnedByAnotherUser(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing fixture ownership requires root")
	}
	path := secureTempDir(t) + "/cursor.json"
	if err := os.WriteFile(path, []byte(`{"version":1,"boxes":{}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chown(path, 1, -1); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadCursorState(path); err == nil {
		t.Fatal("loadCursorState() error = nil, want cursor owner error")
	}
}

func TestCredentialManagerRejectsCredentialDirectoryOwnedByAnotherUser(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing fixture ownership requires root")
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := validateSecureCredentialDirectory(dir); err != nil {
		t.Fatalf("secure fixture rejected before ownership change: %v", err)
	}
	if err := os.Chown(dir, 1, -1); err != nil {
		t.Fatal(err)
	}
	if err := validateSecureCredentialDirectory(dir); err == nil {
		t.Fatal("validateSecureCredentialDirectory() error = nil, want owner error")
	}
}
