package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

const (
	cliOAuthClientID       = "khMWSVDVSq78oyKA3KtxmYRv"
	cliInstallID           = "hey-cli"
	maxCredentialFileBytes = 1 << 20
	maxTokenResponseBytes  = 1 << 20
)

type cliCredential struct {
	AccessToken   string `json:"access_token"`
	RefreshToken  string `json:"refresh_token"`
	ExpiresAt     int64  `json:"expires_at"`
	OAuthType     string `json:"oauth_type,omitempty"`
	TokenEndpoint string `json:"token_endpoint"`
	SessionCookie string `json:"session_cookie,omitempty"`
	Scope         string `json:"scope,omitempty"`
	UserID        string `json:"user_id,omitempty"`
}

type credentialManager struct {
	configDir  string
	origin     string
	httpClient *http.Client
	mu         sync.Mutex
	lastToken  string
}

func newCredentialManager(configDir, origin string, httpClient *http.Client) *credentialManager {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}
	return &credentialManager{
		configDir:  configDir,
		origin:     strings.TrimRight(origin, "/"),
		httpClient: httpClient,
	}
}

func (m *credentialManager) AccessToken(ctx context.Context) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	credential, _, err := m.load()
	if err != nil {
		return "", fmt.Errorf("load credentials: %w", err)
	}
	m.lastToken = credential.AccessToken
	if credential.ExpiresAt > 0 && time.Now().Unix() >= credential.ExpiresAt-300 {
		if err := m.refreshLocked(ctx, credential.AccessToken); err != nil {
			return "", err
		}
		credential, _, err = m.load()
		if err != nil {
			return "", fmt.Errorf("load refreshed credentials: %w", err)
		}
	}
	if credential.AccessToken == "" {
		return "", fmt.Errorf("credentials contain no access token")
	}
	m.lastToken = credential.AccessToken
	return credential.AccessToken, nil
}

func (m *credentialManager) Refresh(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.refreshLocked(ctx, m.lastToken)
}

func (m *credentialManager) refreshLocked(ctx context.Context, observedToken string) error {
	unlock, err := m.lockCredentials()
	if err != nil {
		return fmt.Errorf("lock credentials for refresh: %w", err)
	}
	defer unlock()
	credential, all, err := m.load()
	if err != nil {
		return fmt.Errorf("load credentials for refresh: %w", err)
	}
	if observedToken != "" && credential.AccessToken != "" && credential.AccessToken != observedToken {
		m.lastToken = credential.AccessToken
		return nil
	}
	if credential.RefreshToken == "" || credential.TokenEndpoint == "" {
		return fmt.Errorf("refresh credentials are incomplete")
	}
	if err := hey.RequireSecureEndpoint(credential.TokenEndpoint); err != nil {
		return fmt.Errorf("token endpoint is not secure")
	}
	form := url.Values{
		"grant_type":    {"refresh_token"},
		"client_id":     {cliOAuthClientID},
		"refresh_token": {credential.RefreshToken},
		"install_id":    {cliInstallID},
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, credential.TokenEndpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return fmt.Errorf("create token refresh request: %w", err)
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	refreshClient := *m.httpClient
	refreshClient.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return errors.New("OAuth token refresh redirects are disabled")
	}
	response, err := refreshClient.Do(request)
	if err != nil {
		return fmt.Errorf("token refresh request failed: %w", err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maxTokenResponseBytes+1))
	if err != nil {
		return fmt.Errorf("read token refresh response: %w", err)
	}
	if len(body) > maxTokenResponseBytes {
		return fmt.Errorf("token refresh response is too large")
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("token refresh failed with status %d", response.StatusCode)
	}
	var token struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int64  `json:"expires_in"`
		Scope        string `json:"scope"`
	}
	if err := json.Unmarshal(body, &token); err != nil || token.AccessToken == "" {
		return fmt.Errorf("token refresh returned invalid data")
	}
	credential.AccessToken = token.AccessToken
	if token.RefreshToken != "" {
		credential.RefreshToken = token.RefreshToken
	}
	if token.ExpiresIn > 0 {
		credential.ExpiresAt = time.Now().Unix() + token.ExpiresIn
	} else {
		credential.ExpiresAt = 0
	}
	if token.Scope != "" {
		credential.Scope = token.Scope
	}
	if err := m.save(all, credential); err != nil {
		return fmt.Errorf("save refreshed credentials: %w", err)
	}
	m.lastToken = credential.AccessToken
	return nil
}

func (m *credentialManager) credentialsPath() string {
	return filepath.Join(m.configDir, "credentials.json")
}

func (m *credentialManager) load() (*cliCredential, map[string]json.RawMessage, error) {
	if err := validateSecureCredentialDirectory(m.configDir); err != nil {
		return nil, nil, err
	}
	pathInfo, err := os.Lstat(m.credentialsPath())
	if err != nil {
		return nil, nil, err
	}
	if !secureCredentialFile(m.credentialsPath(), pathInfo) {
		return nil, nil, fmt.Errorf("credential file is insecure")
	}
	file, err := os.Open(m.credentialsPath())
	if err != nil {
		return nil, nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, nil, err
	}
	if info.Size() > maxCredentialFileBytes {
		return nil, nil, fmt.Errorf("credential file is insecure or oversized")
	}
	decoder := json.NewDecoder(io.LimitReader(file, maxCredentialFileBytes+1))
	var all map[string]json.RawMessage
	if err := decoder.Decode(&all); err != nil {
		return nil, nil, fmt.Errorf("invalid credential file")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, nil, fmt.Errorf("invalid credential file")
	}
	raw, ok := all[m.origin]
	if !ok {
		return nil, nil, fmt.Errorf("credentials not found")
	}
	var credential cliCredential
	if err := json.Unmarshal(raw, &credential); err != nil {
		return nil, nil, fmt.Errorf("invalid credential entry")
	}
	return &credential, all, nil
}

func (m *credentialManager) save(all map[string]json.RawMessage, credential *cliCredential) error {
	if all == nil || credential == nil || credential.AccessToken == "" {
		return fmt.Errorf("invalid credential state")
	}
	raw, err := json.Marshal(credential)
	if err != nil {
		return err
	}
	all[m.origin] = raw
	data, err := json.MarshalIndent(all, "", "  ")
	if err != nil {
		return err
	}
	if len(data) > maxCredentialFileBytes {
		return fmt.Errorf("credential file is too large")
	}
	if err := os.MkdirAll(m.configDir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(m.configDir, ".credentials-*.tmp")
	if err != nil {
		return err
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
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := replaceFile(tmpPath, m.credentialsPath()); err != nil {
		return err
	}
	ok = true
	return syncParentDirectory(m.configDir)
}
