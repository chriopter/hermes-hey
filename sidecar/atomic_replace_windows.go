//go:build windows

package main

import "golang.org/x/sys/windows"

func replaceFile(source, destination string) error {
	from, err := windows.UTF16PtrFromString(source)
	if err != nil {
		return err
	}
	to, err := windows.UTF16PtrFromString(destination)
	if err != nil {
		return err
	}
	return windows.MoveFileEx(from, to, windows.MOVEFILE_REPLACE_EXISTING|windows.MOVEFILE_WRITE_THROUGH)
}

// MoveFileEx with MOVEFILE_WRITE_THROUGH provides replacement durability on Windows.
// Opening a directory and calling Sync is unsupported there.
func syncParentDirectory(string) error {
	return nil
}
