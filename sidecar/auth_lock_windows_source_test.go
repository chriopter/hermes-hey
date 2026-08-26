package main

import (
	"os"
	"strings"
	"testing"
)

func TestWindowsSecuritySourceInspectsOwnerAndDACL(t *testing.T) {
	data, err := os.ReadFile("auth_lock_windows.go")
	if err != nil {
		t.Fatal(err)
	}
	source := string(data)
	for _, required := range []string{
		"GetNamedSecurityInfo",
		"OWNER_SECURITY_INFORMATION",
		"DACL_SECURITY_INFORMATION",
		"GetCurrentProcessToken",
		"GetAce",
	} {
		if !strings.Contains(source, required) {
			t.Fatalf("auth_lock_windows.go does not use %s", required)
		}
	}
	if strings.Contains(source, "func ownedByCurrentUser(os.FileInfo) bool             { return true }") {
		t.Fatal("Windows ownership check is permissive")
	}
}

func TestUnsupportedPlatformSecuritySourceFailsClosed(t *testing.T) {
	data, err := os.ReadFile("auth_lock_other.go")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "return true") {
		t.Fatal("unsupported-platform security checks are permissive")
	}
}
