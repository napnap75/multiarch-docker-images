package main

import (
	"log"
	"os"

	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/pbapp"
	"github.com/napnap75/archivist/internal/pipeline"
	"github.com/napnap75/archivist/internal/storage"
	"github.com/pocketbase/pocketbase"
)

// EnvConfig is the technical configuration, from environment variables (US-01).
type EnvConfig struct {
	S3Endpoint    string
	S3AccessKey   string
	S3SecretKey   string
	S3Bucket      string
	S3UseSSL      bool
	AdminEmail    string
	AdminPassword string
	PollCron      string
	HTTPAddr      string
	ConfigPath    string
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// LoadEnvConfig reads and validates the mandatory environment variables (US-01).
func LoadEnvConfig() (*EnvConfig, error) {
	c := &EnvConfig{
		S3Endpoint:    os.Getenv("ARCHIVIST_S3_ENDPOINT"),
		S3AccessKey:   os.Getenv("ARCHIVIST_S3_ACCESS_KEY"),
		S3SecretKey:   os.Getenv("ARCHIVIST_S3_SECRET_KEY"),
		S3Bucket:      os.Getenv("ARCHIVIST_S3_BUCKET"),
		AdminEmail:    os.Getenv("ARCHIVIST_ADMIN_EMAIL"),
		AdminPassword: os.Getenv("ARCHIVIST_ADMIN_PASSWORD"),
	}
	for k, v := range map[string]string{
		"ARCHIVIST_S3_ENDPOINT":    c.S3Endpoint,
		"ARCHIVIST_S3_ACCESS_KEY":  c.S3AccessKey,
		"ARCHIVIST_S3_SECRET_KEY":  c.S3SecretKey,
		"ARCHIVIST_S3_BUCKET":      c.S3Bucket,
		"ARCHIVIST_ADMIN_EMAIL":    c.AdminEmail,
		"ARCHIVIST_ADMIN_PASSWORD": c.AdminPassword,
	} {
		if v == "" {
			log.Fatalf("missing mandatory environment variable %s", k)
		}
	}
	c.S3UseSSL = env("ARCHIVIST_S3_USE_SSL", "true") != "false"
	c.PollCron = env("ARCHIVIST_POLL_INTERVAL", "* * * * *")
	c.HTTPAddr = env("ARCHIVIST_HTTP_ADDR", "0.0.0.0:8090")
	c.ConfigPath = env("ARCHIVIST_CONFIG", "config.jsonc")
	return c, nil
}

func main() {
	envCfg, err := LoadEnvConfig()
	if err != nil {
		log.Fatal(err)
	}

	// US-02: load and validate the functional configuration. Full rejection,
	// the process does not continue with a partial configuration (US-05).
	cfg, err := config.Load(envCfg.ConfigPath)
	if err != nil {
		log.Fatalf("invalid functional configuration: %v", err)
	}
	log.Printf("configuration loaded: %d fields, %d top-level rules, %d templates",
		len(cfg.Fields), len(cfg.Rules), len(cfg.Templates))

	// US-01: verify S3 reachability and versioning before anything else.
	s3, err := storage.New(envCfg.S3Endpoint, envCfg.S3AccessKey, envCfg.S3SecretKey, envCfg.S3Bucket, envCfg.S3UseSSL)
	if err != nil {
		log.Fatalf("S3 client: %v", err)
	}

	app := pocketbase.New()

	// US-03 / US-04 / US-01: collections migration and superuser account.
	if err := pbapp.Setup(app, envCfg.AdminEmail, envCfg.AdminPassword, cfg); err != nil {
		log.Fatalf("pocketbase setup: %v", err)
	}

	// US-22 / US-23 / US-04: request hooks on documents and document_events.
	pbapp.BindHooks(app, cfg, s3)

	// US-10..US-17: the poller, on the configured cron.
	app.CronAdd(envCfg.PollCron, "archivist-poller", func() {
		pipeline.RunPoll(app, cfg, s3)
	})

	app.RootCmd.PersistentFlags().Set("http", envCfg.HTTPAddr)
	if err := app.Start(); err != nil {
		log.Fatal(err)
	}
}
