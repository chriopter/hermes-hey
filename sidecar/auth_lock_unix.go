//go:build unix

package main

import (
	"os"
	"syscall"
)

func acquireCredentialLock(file *os.File) error {
	return syscall.Flock(int(file.Fd()), syscall.LOCK_EX)
}

func releaseCredentialLock(file *os.File) error {
	return syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
}

func ownedByCurrentUser(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid())
}

func secureCredentialDirectory(_ string, info os.FileInfo) bool {
	return info.IsDir() && info.Mode().Perm() == 0o700 && ownedByCurrentUser(info)
}

func secureCredentialFile(_ string, info os.FileInfo) bool {
	return info.Mode().IsRegular() && info.Mode().Perm() == 0o600 && ownedByCurrentUser(info)
}

func secureCursorFile(_ string, info os.FileInfo) bool {
	return info.Mode().IsRegular() && info.Mode().Perm() == 0o600 && ownedByCurrentUser(info)
}
