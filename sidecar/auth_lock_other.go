//go:build !unix && !windows

package main

import "os"

func acquireCredentialLock(*os.File) error { return nil }
func releaseCredentialLock(*os.File) error { return nil }

func secureCredentialDirectory(string, os.FileInfo) bool { return false }
func secureCredentialFile(string, os.FileInfo) bool      { return false }
func secureCursorFile(string, os.FileInfo) bool          { return false }
