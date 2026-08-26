package main

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
	hey "github.com/basecamp/hey-sdk/go/pkg/hey"
)

type sdkAdapter struct {
	client *hey.Client
}

func newSDKClients(ctx context.Context, accountID int64, configDir, baseURL string) (*hey.Client, *hey.Client, *generated.Identity, error) {
	cfg := hey.DefaultConfig()
	cfg.BaseURL = baseURL
	cfg.CacheEnabled = false
	httpClient := &http.Client{Timeout: 30 * time.Second}
	provider := newCredentialManager(configDir, cfg.BaseURL, httpClient)
	root := hey.NewClient(cfg, provider,
		hey.WithTimeout(30*time.Second),
		hey.WithMaxRetries(3),
		hey.WithMaxPages(maxSDKPages),
		hey.WithMaxResponseBodyBytes(maxSDKBodyBytes),
	)
	identity, err := root.Identity().GetIdentity(ctx)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("fetch identity: %w", err)
	}
	if err := verifyAccount(identity, accountID); err != nil {
		return nil, nil, nil, err
	}
	scoped, err := root.ForAccount(ctx, accountID)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("select account: %w", err)
	}
	return root, scoped, identity, nil
}

func verifyAccount(identity *generated.Identity, accountID int64) error {
	if identity == nil || accountID <= 0 {
		return fmt.Errorf("identity account mismatch")
	}
	for _, account := range identity.Accounts {
		if account.Id == accountID && (account.Status == "active" || account.Status == "inactive" && (account.Purpose == "work" || account.Purpose == "domains")) {
			return nil
		}
	}
	return fmt.Errorf("identity account mismatch")
}

func (a sdkAdapter) ListBoxes(ctx context.Context) ([]generated.Box, error) {
	boxes, err := a.client.Boxes().List(ctx)
	if err != nil {
		return nil, err
	}
	if boxes == nil {
		return nil, fmt.Errorf("boxes returned no data")
	}
	return *boxes, nil
}

func (a sdkAdapter) AllChanges(ctx context.Context, boxID int64, cursor hey.PostingChangesCursor) (*hey.PostingChanges, error) {
	return a.client.Postings().AllChanges(ctx, boxID, cursor)
}

func (a sdkAdapter) TopicEntries(ctx context.Context, topicID int64) ([]generated.Entry, error) {
	if topicID <= 0 {
		return nil, fmt.Errorf("topic ID must be positive")
	}
	var entries []generated.Entry
	page := ""
	for count := 0; count < maxSDKPages; count++ {
		result, err := a.client.Topics().GetEntriesPage(ctx, topicID, page)
		if err != nil {
			return nil, err
		}
		if result == nil {
			return nil, fmt.Errorf("topic entries returned no data")
		}
		entries = append(entries, result.Entries...)
		if result.NextPage == "" {
			return entries, nil
		}
		page = result.NextPage
	}
	return nil, fmt.Errorf("topic entries pagination limit exceeded")
}

func (a sdkAdapter) Message(ctx context.Context, id int64) (*generated.Message, error) {
	if id <= 0 {
		return nil, fmt.Errorf("message ID must be positive")
	}
	return a.client.Messages().Get(ctx, id)
}

func (a sdkAdapter) NewReply(ctx context.Context, id int64) (*generated.MessageDraft, error) {
	if id <= 0 {
		return nil, fmt.Errorf("entry ID must be positive")
	}
	return a.client.Entries().NewReply(ctx, id)
}

func (a sdkAdapter) DefaultSenderID(ctx context.Context) (int64, error) {
	return a.client.DefaultSenderID(ctx)
}

func (a sdkAdapter) CreateReply(ctx context.Context, id int64, content string, to, cc, bcc []string) error {
	if id <= 0 {
		return fmt.Errorf("entry ID must be positive")
	}
	return a.client.Entries().CreateReply(ctx, id, content, to, cc, bcc)
}

const (
	maxSDKPages     = 100
	maxSDKBodyBytes = 8 << 20
)
