package main

import (
	"fmt"
	"os"
	"path/filepath"
)

func (m *credentialManager) lockCredentials() (func(), error) {
	if err := ensureSecureCredentialDirectory(m.configDir); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(filepath.Join(m.configDir, "credentials.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := acquireCredentialLock(file); err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("lock credentials: %w", err)
	}
	return func() {
		_ = releaseCredentialLock(file)
		_ = file.Close()
	}, nil
}

func ensureSecureCredentialDirectory(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return err
	}
	return validateSecureCredentialDirectory(path)
}

func validateSecureCredentialDirectory(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	if !secureCredentialDirectory(path, info) {
		return fmt.Errorf("credential directory is insecure")
	}
	return nil
}
