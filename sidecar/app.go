package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/basecamp/hey-sdk/go/pkg/generated"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	args := os.Args[1:]
	err := run(ctx, args, os.Stdin, os.Stdout)
	if err == nil || errors.Is(err, context.Canceled) {
		return
	}
	if len(args) > 0 && args[0] == "watch" {
		_ = writeNDJSON(os.Stdout, struct {
			Type  string `json:"type"`
			Error string `json:"error"`
		}{Type: "fatal", Error: redactError(err)})
	}
	_, _ = fmt.Fprintln(os.Stderr, redactError(err))
	os.Exit(exitCode(err))
}

func exitCode(err error) int {
	if isSafeRetry(err) {
		return 75
	}
	return 1
}

func run(ctx context.Context, args []string, in io.Reader, out io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("a mode is required")
	}
	switch args[0] {
	case "verify":
		fs := newFlagSet("verify")
		accountText, ownEmail, configDir, baseURL := commonFlags(fs, true)
		if err := fs.Parse(args[1:]); err != nil || fs.NArg() != 0 {
			return fmt.Errorf("invalid verify arguments")
		}
		account, err := validateCommon(*accountText, *configDir, *baseURL)
		if err != nil {
			return err
		}
		if strings.TrimSpace(*ownEmail) == "" {
			return fmt.Errorf("own email is required")
		}
		root, scoped, identity, err := newSDKClients(ctx, account, *configDir, *baseURL)
		_ = root
		_ = scoped
		if err != nil {
			return err
		}
		if err := verifyIdentity(identity, account, *ownEmail); err != nil {
			return err
		}
		return writeNDJSON(out, newVerifyResponse(account, strings.TrimSpace(*ownEmail)))
	case "watch":
		fs := newFlagSet("watch")
		accountText, ownEmail, configDir, baseURL := commonFlags(fs, true)
		statePath := fs.String("cursor-state", "", "")
		interval := fs.Duration("poll-interval", 30*time.Second, "")
		if err := fs.Parse(args[1:]); err != nil || fs.NArg() != 0 {
			return fmt.Errorf("invalid watch arguments")
		}
		account, err := validateCommon(*accountText, *configDir, *baseURL)
		if err != nil {
			return err
		}
		if strings.TrimSpace(*ownEmail) == "" || *statePath == "" || *interval <= 0 {
			return fmt.Errorf("invalid watch arguments")
		}
		_, scoped, identity, err := newSDKClients(ctx, account, *configDir, *baseURL)
		if err != nil {
			return err
		}
		if err := verifyIdentity(identity, account, *ownEmail); err != nil {
			return err
		}
		engine := watchEngine{api: sdkAdapter{client: scoped}, statePath: *statePath, ownEmail: *ownEmail, in: in, out: out}
		if err := engine.initialize(ctx); err != nil {
			return err
		}
		if err := engine.poll(ctx); err != nil {
			return err
		}
		ticker := time.NewTicker(*interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-ticker.C:
				if err := engine.poll(ctx); err != nil {
					return err
				}
			}
		}
	case "reply":
		fs := newFlagSet("reply")
		accountText, _, configDir, baseURL := commonFlags(fs, false)
		threadText := fs.String("thread-id", "", "")
		if err := fs.Parse(args[1:]); err != nil || fs.NArg() != 0 {
			return fmt.Errorf("invalid reply arguments")
		}
		account, err := validateCommon(*accountText, *configDir, *baseURL)
		if err != nil {
			return err
		}
		threadID, err := strconv.ParseInt(*threadText, 10, 64)
		if err != nil || threadID <= 0 {
			return fmt.Errorf("thread ID must be positive")
		}
		_, scoped, _, err := newSDKClients(ctx, account, *configDir, *baseURL)
		if err != nil {
			return classifyReplyClientError(err)
		}
		return runReply(ctx, sdkAdapter{client: scoped}, threadID, in, out)
	default:
		return fmt.Errorf("unknown mode")
	}
}

func classifyReplyClientError(err error) error {
	wrapped := fmt.Errorf("initialize reply client: %w", err)
	if isTransientReadError(err) {
		return safeRetry(wrapped)
	}
	return wrapped
}

func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

type verifyResponse struct {
	OK              bool   `json:"ok"`
	ProtocolVersion int    `json:"protocol_version"`
	SDKVersion      string `json:"sdk_version"`
	Account         int64  `json:"account"`
	Email           string `json:"email"`
}

func newVerifyResponse(account int64, email string) verifyResponse {
	return verifyResponse{
		OK:              true,
		ProtocolVersion: protocolVersion,
		SDKVersion:      sdkVersion,
		Account:         account,
		Email:           email,
	}
}

const (
	protocolVersion = 1
	sdkVersion      = "0.24.0"
)

func commonFlags(fs *flag.FlagSet, withEmail bool) (*string, *string, *string, *string) {
	account := fs.String("account", "", "")
	ownEmail := new(string)
	if withEmail {
		ownEmail = fs.String("own-email", "", "")
	}
	configDir := fs.String("config-dir", "", "")
	baseURL := fs.String("base-url", "https://app.hey.com", "")
	return account, ownEmail, configDir, baseURL
}

func validateCommon(accountText, configDir, baseURL string) (int64, error) {
	account, err := parseCanonicalAccount(accountText)
	if err != nil {
		return 0, err
	}
	if strings.TrimSpace(configDir) == "" {
		return 0, fmt.Errorf("config directory is required")
	}
	if err := validateBaseURL(baseURL); err != nil {
		return 0, err
	}
	return account, nil
}

func parseCanonicalAccount(value string) (int64, error) {
	if len(value) == 0 || value[0] < '1' || value[0] > '9' {
		return 0, fmt.Errorf("invalid arguments")
	}
	for i := 1; i < len(value); i++ {
		if value[i] < '0' || value[i] > '9' {
			return 0, fmt.Errorf("invalid arguments")
		}
	}
	account, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid arguments")
	}
	return account, nil
}

func validateBaseURL(value string) error {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Host == "" || parsed.User != nil || (parsed.Path != "" && parsed.Path != "/") || parsed.RawQuery != "" || parsed.Fragment != "" {
		return fmt.Errorf("invalid base URL")
	}
	if parsed.Scheme == "https" {
		return nil
	}
	host := parsed.Hostname()
	if parsed.Scheme == "http" && (host == "localhost" || net.ParseIP(host) != nil && net.ParseIP(host).IsLoopback()) {
		return nil
	}
	return fmt.Errorf("base URL must use HTTPS")
}

func verifyIdentity(identity *generated.Identity, accountID int64, ownEmail string) error {
	if identity == nil || accountID <= 0 || strings.TrimSpace(ownEmail) == "" {
		return fmt.Errorf("identity verification failed")
	}
	accountFound := false
	for _, account := range identity.Accounts {
		if account.Id == accountID && (account.Status == "active" || account.Status == "inactive" && (account.Purpose == "work" || account.Purpose == "domains")) {
			accountFound = true
			break
		}
	}
	if !accountFound {
		return fmt.Errorf("identity account mismatch")
	}
	want := strings.TrimSpace(ownEmail)
	for _, user := range identity.AllUsers {
		if user.AccountId == accountID && strings.EqualFold(strings.TrimSpace(user.Contact.EmailAddress), want) {
			return nil
		}
	}
	return fmt.Errorf("identity email mismatch")
}

func redactError(err error) string {
	if err == nil {
		return ""
	}
	message := strings.ToLower(err.Error())
	switch {
	case strings.Contains(message, "auth") || strings.Contains(message, "credential") || strings.Contains(message, "unauthorized"):
		return "authentication failed"
	case strings.Contains(message, "cursor"):
		return "cursor state error"
	case strings.Contains(message, "ack"):
		return "acknowledgement failed"
	case strings.Contains(message, "identity") || strings.Contains(message, "account") || strings.Contains(message, "email"):
		return "identity verification failed"
	case strings.Contains(message, "reply"):
		return "reply operation failed"
	case strings.Contains(message, "argument") || strings.Contains(message, "required") || strings.Contains(message, "positive") || strings.Contains(message, "base url"):
		return "invalid arguments"
	default:
		return "sidecar operation failed"
	}
}
