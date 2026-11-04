// Package e2e implements the E2E regression gate of arch.md section 10.
//
// The suite is one narrative: it starts a disposable Garage and the image
// under test, then follows documents through the whole pipeline - import,
// duplicate, pending, correction, validation, append-only, recovery,
// restart. It is a regression gate for dependency upgrades (PocketBase,
// minio-go, ledongthuc/pdf, expr, Garage), not an acceptance proof.
//
// Run it from the archivist/ directory against an already built image:
//
//	E2E_IMAGE=archivist:e2e go test ./e2e -v -count=1
//
// Without E2E_IMAGE the suite skips. E2E_KEEP=1 leaves the containers
// running for debugging. GARAGE_IMAGE overrides the Garage image.
package e2e

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

const (
	appC      = "archivist-e2e-app"
	garageC   = "archivist-e2e-garage"
	network   = "archivist-e2e"
	baseURL   = "http://127.0.0.1:8090"
	s3Addr    = "127.0.0.1:3900"
	bucket    = "archivist-e2e"
	adminMail = "admin@example.net"
	adminPass = "e2e-password-42"
)

var (
	token    string
	s3       *minio.Client
	s3KeyID  string
	s3Secret string
	dir      string
)

func TestMain(m *testing.M) {
	image := os.Getenv("E2E_IMAGE")
	if image == "" {
		fmt.Println("E2E: E2E_IMAGE not set, skipping (arch.md section 10)")
		os.Exit(0)
	}
	code := 1
	defer func() { os.Exit(code) }()
	if err := setup(image); err != nil {
		fmt.Println("E2E setup failed:", err)
		if out, _ := run("docker", "logs", appC); out != "" {
			fmt.Println("--- app logs ---")
			fmt.Println(out)
		}
		return
	}
	code = m.Run()
	if code != 0 {
		if out, _ := run("docker", "logs", appC); out != "" {
			fmt.Println("--- app logs ---")
			fmt.Println(out)
		}
	}
	if os.Getenv("E2E_KEEP") == "1" {
		fmt.Println("E2E: E2E_KEEP=1, containers left running:", appC, garageC)
		return
	}
	teardown()
}

func setup(image string) error {
	var err error
	dir, err = os.MkdirTemp("", "archivist-e2e-*")
	if err != nil {
		return err
	}
	if err := startGarage(); err != nil {
		return err
	}
	return startApp(image)
}

// startGarage runs a disposable single-node Garage on a private docker
// network and bootstraps layout, bucket and key (arch.md section 10).
func startGarage() error {
	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return err
	}
	toml := fmt.Sprintf(`metadata_dir = "/tmp/meta"
	data_dir = "/tmp/data"
	replication_factor = 1
	rpc_bind_addr = "[::]:3901"
	rpc_public_addr = "127.0.0.1:3901"
	rpc_secret = "%s"
	bootstrap_peers = []
	[s3_api]
	s3_region = "garage"
	api_bind_addr = "[::]:3900"
	root_domain = ".s3.garage"
`, hex.EncodeToString(secret))
	tomlPath := filepath.Join(dir, "garage.toml")
	if err := os.WriteFile(tomlPath, []byte(toml), 0o644); err != nil {
		return err
	}
	gImage := os.Getenv("GARAGE_IMAGE")
	if gImage == "" {
		gImage = "dxflrs/garage:v2.5.0"
	}
	_, _ = run("docker", "network", "create", network) // already exists is fine
	if _, err := run("docker", "run", "-d", "--name", garageC, "--network", network,
		"-p", "127.0.0.1:3900:3900", "-v", tomlPath+":/etc/garage.toml", gImage); err != nil {
		return fmt.Errorf("garage container: %w", err)
	}
	if err := retry(60*time.Second, func() error { _, err := garage("status"); return err }); err != nil {
		return fmt.Errorf("garage never became ready: %w", err)
	}
	status, _ := garage("status")
	nodeID := firstHexToken(status)
	if nodeID == "" {
		return fmt.Errorf("no node id in garage status:\n%s", status)
	}
	if _, err := garage("layout", "assign", "-z", "dc1", "-c", "1G", nodeID); err != nil {
		return err
	}
	if _, err := garage("layout", "apply", "--version", "1"); err != nil {
		return err
	}
	if _, err := garage("bucket", "create", bucket); err != nil {
		return err
	}
	keyOut, err := garage("key", "create", bucket)
	if err != nil {
		return err
	}
	s3KeyID = lineValue(keyOut, "Key ID:")
	s3Secret = lineValue(keyOut, "Secret key:")
	if s3KeyID == "" || s3Secret == "" {
		return fmt.Errorf("cannot parse garage key output:\n%s", keyOut)
	}
	if _, err := garage("bucket", "allow", "--read", "--write", bucket, "--key", bucket); err != nil {
		return err
	}
	s3c, err := minio.New(s3Addr, &minio.Options{
		Creds:  credentials.NewStaticV4(s3KeyID, s3Secret, ""),
		Secure: false,
		Region: "garage",
	})
	if err != nil {
		return err
	}
	s3 = s3c
	return nil
}

// startApp runs the image under test wired to Garage, with the cron
// disabled: the tests drive the poller through POST /api/archivist/poll.
func startApp(image string) error {
	cfgPath := filepath.Join(dir, "config.jsonc")
	if err := os.WriteFile(cfgPath, []byte(testConfig), 0o644); err != nil {
		return err
	}
	dataPath := filepath.Join(dir, "pb_data")
	if err := os.Mkdir(dataPath, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(dataPath, 0o777); err != nil {
		return err
	}
	env := []string{
		"ARCHIVIST_S3_ENDPOINT=" + garageC + ":3900",
		"ARCHIVIST_S3_ACCESS_KEY=" + s3KeyID,
		"ARCHIVIST_S3_SECRET_KEY=" + s3Secret,
		"ARCHIVIST_S3_BUCKET=" + bucket,
		"ARCHIVIST_S3_REGION=garage",
		"ARCHIVIST_S3_USE_SSL=false",
		"ARCHIVIST_ADMIN_EMAIL=" + adminMail,
		"ARCHIVIST_ADMIN_PASSWORD=" + adminPass,
		"ARCHIVIST_CONFIG=/config.jsonc",
		// Never fires (February 31st): the timing stays deterministic.
		"ARCHIVIST_POLL_INTERVAL=0 0 31 2 *",
	}
	args := []string{"run", "-d", "--name", appC, "--network", network,
		"-p", "127.0.0.1:8090:8090",
		"-v", cfgPath + ":/config.jsonc",
		"-v", dataPath + ":/pb_data",
		"-v", dataPath + ":/app/pb_data"}
	for _, e := range env {
		args = append(args, "-e", e)
	}
	args = append(args, image)
	if _, err := run("docker", args...); err != nil {
		return fmt.Errorf("app container: %w", err)
	}
	if err := retry(60*time.Second, checkHealth); err != nil {
		if out, _ := run("docker", "logs", appC); out != "" {
			return fmt.Errorf("app never became healthy; logs:\n%s", out)
		}
		return err
	}
	if err := authenticate(); err != nil {
		return err
	}
	return nil
}

func authenticate() error {
	token = ""
	_, body, err := api(http.MethodPost, "/api/collections/_superusers/auth-with-password",
		map[string]any{"identity": adminMail, "password": adminPass})
	if err != nil {
		return err
	}
	tok, _ := body["token"].(string)
	if tok == "" {
		return fmt.Errorf("no superuser token in response: %v", body)
	}
	token = tok
	return nil
}

func checkHealth() error {
	resp, err := http.Get(baseURL + "/api/health")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != 200 {
		return fmt.Errorf("health: %d", resp.StatusCode)
	}
	return nil
}

func teardown() {
	run("docker", "rm", "-f", appC, garageC)
	run("docker", "network", "rm", network)
	os.RemoveAll(dir)
}

// --- small plumbing ----------------------------------------------------

func run(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	err := cmd.Run()
	return out.String(), err
}

func garage(args ...string) (string, error) {
	return run("docker", append([]string{"exec", garageC, "/garage"}, args...)...)
}

func retry(timeout time.Duration, f func() error) error {
	deadline := time.Now().Add(timeout)
	var err error
	for time.Now().Before(deadline) {
		if err = f(); err == nil {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return err
}

var hexRe = regexp.MustCompile(`^[0-9a-f]{8,}$`)

// firstHexToken extracts the node id from garage status output.
func firstHexToken(s string) string {
	for _, line := range strings.Split(s, "\n") {
		fields := strings.Fields(line)
		if len(fields) > 0 && hexRe.MatchString(fields[0]) {
			return fields[0]
		}
	}
	return ""
}

func lineValue(s, prefix string) string {
	for _, line := range strings.Split(s, "\n") {
		t := strings.TrimSpace(line)
		if strings.HasPrefix(t, prefix) {
			return strings.TrimSpace(strings.TrimPrefix(t, prefix))
		}
	}
	return ""
}

// --- PocketBase and S3 clients ------------------------------------------

func api(method, path string, body any) (int, map[string]any, error) {
	var req *http.Request
	var err error
	if body != nil {
		var data []byte
		data, err = json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		req, err = http.NewRequest(method, baseURL+path, bytes.NewReader(data))
	} else {
		req, err = http.NewRequest(method, baseURL+path, nil)
	}
	if err != nil {
		return 0, nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var out map[string]any
	json.Unmarshal(data, &out)
	return resp.StatusCode, out, nil
}

func call(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	status, out, err := api(method, path, body)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	return status, out
}

// poll triggers one poll pass through the superuser-only route (US-10
// waits for size/ETag stability across two passes, so a fresh upload
// needs two calls).
func poll(t *testing.T) {
	t.Helper()
	status, out := call(t, http.MethodPost, "/api/archivist/poll", nil)
	if status != 200 {
		t.Fatalf("poll route: status %d: %v", status, out)
	}
}

func listRecords(t *testing.T, collection, filter, sort string) []map[string]any {
	t.Helper()
	q := url.Values{}
	if filter != "" {
		q.Set("filter", filter)
	}
	if sort != "" {
		q.Set("sort", sort)
	}
	q.Set("perPage", "200")
	status, out := call(t, http.MethodGet, "/api/collections/"+collection+"/records?"+q.Encode(), nil)
	if status != 200 {
		t.Fatalf("list %s: status %d: %v", collection, status, out)
	}
	items, _ := out["items"].([]any)
	recs := make([]map[string]any, 0, len(items))
	for _, it := range items {
		recs = append(recs, it.(map[string]any))
	}
	return recs
}

func oneRecord(t *testing.T, collection, filter string) map[string]any {
	t.Helper()
	recs := listRecords(t, collection, filter, "")
	if len(recs) != 1 {
		t.Fatalf("want exactly 1 record in %s for %q, got %d", collection, filter, len(recs))
	}
	return recs[0]
}

func eventsOf(t *testing.T, docID string) []map[string]any {
	t.Helper()
	return listRecords(t, "document_events", "(document='"+docID+"')", "timestamp")
}

func eventPayload(t *testing.T, rec map[string]any) map[string]any {
	t.Helper()
	var data []byte
	switch raw := rec["payload"].(type) {
	case string:
		data = []byte(raw)
	case map[string]any:
		var err error
		data, err = json.Marshal(raw)
		if err != nil {
			t.Fatalf("event payload is not JSON: %v", err)
		}
	default:
		t.Fatalf("unexpected event payload type: %T", rec["payload"])
	}
	var p map[string]any
	if err := json.Unmarshal(data, &p); err != nil {
		t.Fatalf("event payload is not JSON: %v", err)
	}
	return p
}

func eventPayloadOfType(t *testing.T, evs []map[string]any, typ string) map[string]any {
	t.Helper()
	for _, e := range evs {
		if e["event_type"] == typ {
			return eventPayload(t, e)
		}
	}
	t.Fatalf("no %s event among %d events", typ, len(evs))
	return nil
}

func pbLogs(t *testing.T) []map[string]any {
	t.Helper()
	status, out := call(t, http.MethodGet, "/api/logs?perPage=200", nil)
	if status != 200 {
		t.Fatalf("logs api: status %d: %v", status, out)
	}
	items, _ := out["items"].([]any)
	recs := make([]map[string]any, 0, len(items))
	for _, it := range items {
		recs = append(recs, it.(map[string]any))
	}
	return recs
}

func s3Exists(t *testing.T, key string) bool {
	t.Helper()
	_, err := s3.StatObject(context.Background(), bucket, key, minio.StatObjectOptions{})
	return err == nil
}

// --- in-memory test PDFs (no fixtures, no generator dependency) ----------

// makePDF builds a minimal valid single-page PDF whose page text is the
// given lines. The classification outcome is the proof that the
// extraction of the application works.
func makePDF(lines []string) []byte {
	var content strings.Builder
	content.WriteString("BT /F1 12 Tf 72 720 Td\n")
	for _, l := range lines {
		content.WriteString("(" + pdfEscape(l) + ") Tj 0 -18 Td\n")
	}
	content.WriteString("ET")
	stream := content.String() + "\n"

	var buf bytes.Buffer
	buf.WriteString("%PDF-1.4\n")
	off := func() int { return buf.Len() }
	o1 := off()
	buf.WriteString("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
	o2 := off()
	buf.WriteString("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
	o3 := off()
	buf.WriteString("3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n")
	o4 := off()
	buf.WriteString(fmt.Sprintf("4 0 obj\n<< /Length %d >>\nstream\n%sendstream\nendobj\n", len(stream), stream))
	o5 := off()
	buf.WriteString("5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
	xref := off()
	buf.WriteString("xref\n0 6\n0000000000 65535 f \n")
	for _, o := range []int{o1, o2, o3, o4, o5} {
		buf.WriteString(fmt.Sprintf("%010d 00000 n \n", o))
	}
	buf.WriteString(fmt.Sprintf("trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", xref))
	return buf.Bytes()
}

func pdfEscape(s string) string {
	return strings.NewReplacer(`\`, `\\`, `(`, `\(`, `)`, `\)`).Replace(s)
}

// putPDF uploads a generated single-page PDF and returns its sha256.
func putPDF(t *testing.T, key string, lines ...string) string {
	t.Helper()
	data := makePDF(lines)
	if _, err := s3.PutObject(context.Background(), bucket, key,
		bytes.NewReader(data), int64(len(data)),
		minio.PutObjectOptions{ContentType: "application/pdf"}); err != nil {
		t.Fatalf("PutObject %s: %v", key, err)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// --- the config.jsonc mounted into the image under test -----------------
// "~" is a stand-in for a backtick: expr string literals are backticked
// (arch.md section 5) and backticks cannot appear in Go raw strings.
var testConfigRaw = `
{
  "fields": [
    { "name": "template", "type": "text" },
    { "name": "emittingCompany", "type": "text", "mandatory": true },
    { "name": "documentType", "type": "text", "mandatory": true },
    { "name": "emissionDate", "type": "date", "mandatory": true },
    { "name": "bankAccount", "type": "text" }
  ],
  "rules": [
    {
      "when": "pages[0] contains ~Releve de compte~",
      "set": [
        { "field": "template", "value": "bank_statement" },
        { "field": "documentType", "value": "Releve de compte" },
        { "field": "emittingCompany", "eval": "firstMatch(~BRED|Fortuneo~, pages[0])" },
        { "field": "emissionDate", "eval": "date(firstMatch(~Date : ([0-9]{2}/[0-9]{2}/[0-9]{4})~, pages[0]), ~02/01/2006~)" },
        { "field": "bankAccount", "eval": "firstMatch(~Compte ([A-Za-z]+)~, pages[0])" }
      ]
    }
  ],
  "templates": {
		"bank_statement": "{emittingCompany}/{emissionDate.Format(~2006-01~)}_{documentType}_{bankAccount}.pdf",
    "default": "files/{documentType}.pdf"
  }
}`

var testConfig = strings.ReplaceAll(testConfigRaw, "~", "`")

func TestE2E(t *testing.T) {

	// 1. Startup (US-01, US-03, US-04): the app started although Garage
	// has no versioning, the collections are queryable, the versioning
	// warning is in the PocketBase logs, and the poll route is
	// superuser-only.
	t.Run("startup: healthy, migrated, versioning warning, protected poll", func(t *testing.T) {
		for _, c := range []string{"documents", "document_events"} {
			status, out := call(t, http.MethodGet, "/api/collections/"+c+"/records?perPage=1", nil)
			if status != 200 {
				t.Fatalf("collection %s not queryable: %d %v", c, status, out)
			}
		}
		// PocketBase writes log batches with a small delay.
		var found bool
		deadline := time.Now().Add(15 * time.Second)
		for !found && time.Now().Before(deadline) {
			for _, l := range pbLogs(t) {
				if msg, _ := l["message"].(string); strings.Contains(msg, "versioning") {
					found = true
				}
			}
			if !found {
				time.Sleep(500 * time.Millisecond)
			}
		}
		if !found {
			t.Fatal("versioning warning missing from the PocketBase logs")
		}
		req, _ := http.NewRequest(http.MethodPost, baseURL+"/api/archivist/poll", nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("poll without superuser auth: want 403, got %d", resp.StatusCode)
		}
	})

	// 2. Import (US-10 to US-17): a valid simulated statement is
	// classified, filed under files/, traced by an imported event with
	// the section-4 payload.
	t.Run("import: inbox directory marker is ignored", func(t *testing.T) {
		if _, err := s3.PutObject(context.Background(), bucket, "inbox/",
			bytes.NewReader(nil), 0, minio.PutObjectOptions{}); err != nil {
			t.Fatalf("PutObject inbox directory marker: %v", err)
		}
		poll(t)
		poll(t)
		if docs := listRecords(t, "documents", "", ""); len(docs) != 0 {
			t.Fatalf("directory marker created %d document(s)", len(docs))
		}
		if s3Exists(t, "pending/") {
			t.Fatal("directory marker was copied to pending/")
		}
	})

	t.Run("import: a valid statement is classified and filed", func(t *testing.T) {
		hash := putPDF(t, "inbox/statement-1.pdf",
			"BRED", "Releve de compte", "Date : 15/12/2025", "Compte Thomas")
		poll(t)
		poll(t) // stability across two passes (US-10)
		doc := oneRecord(t, "documents", "(sha256='"+hash+"')")
		want := "files/BRED/2025-12_Releve de compte_Thomas.pdf"
		if doc["status"] != "processed" {
			t.Fatalf("status: %v", doc["status"])
		}
		if doc["file_key"] != want {
			t.Fatalf("file_key: %v, want %v", doc["file_key"], want)
		}
		if !s3Exists(t, want) {
			t.Fatalf("object %s missing from S3", want)
		}
		if s3Exists(t, "inbox/statement-1.pdf") {
			t.Fatal("source object still in inbox/")
		}
		p := eventPayloadOfType(t, eventsOf(t, doc["id"].(string)), "imported")
		before, _ := p["before"].(map[string]any)
		after, _ := p["after"].(map[string]any)
		if before["file_key"] != "inbox/statement-1.pdf" {
			t.Fatalf("before.file_key: %v", before)
		}
		if after["file_key"] != want || after["status"] != "processed" {
			t.Fatalf("after: %v", after)
		}
		if _, has := p["errors"]; has {
			t.Fatalf("unexpected errors: %v", p["errors"])
		}
	})

	// 3. Duplicate (US-12): same content re-uploaded, deleted from
	// inbox/, no record, no event.
	t.Run("duplicate: same content is dropped, no record, no event", func(t *testing.T) {
		putPDF(t, "inbox/statement-1-copy.pdf",
			"BRED", "Releve de compte", "Date : 15/12/2025", "Compte Thomas")
		poll(t)
		poll(t)
		if s3Exists(t, "inbox/statement-1-copy.pdf") {
			t.Fatal("duplicate not deleted from inbox/")
		}
		docs := listRecords(t, "documents", "", "")
		if len(docs) != 1 {
			t.Fatalf("want 1 document, got %d", len(docs))
		}
		if evs := eventsOf(t, docs[0]["id"].(string)); len(evs) != 1 {
			t.Fatalf("a duplicate must not create events, got %d", len(evs))
		}
	})

	// 4. Pending (US-13 to US-15): a missing mandatory date routes the
	// document to pending/ with the causes in the imported event and the
	// recognized fields kept for the correction form.
	t.Run("pending: a missing mandatory date routes to review", func(t *testing.T) {
		hash := putPDF(t, "inbox/no-date.pdf", "BRED", "Releve de compte", "Compte Thomas")
		poll(t)
		poll(t)
		doc := oneRecord(t, "documents", "(sha256='"+hash+"')")
		if doc["status"] != "pending" {
			t.Fatalf("status: %v", doc["status"])
		}
		if doc["file_key"] != "pending/no-date.pdf" {
			t.Fatalf("file_key: %v", doc["file_key"])
		}
		p := eventPayloadOfType(t, eventsOf(t, doc["id"].(string)), "imported")
		errs, ok := p["errors"].([]any)
		if !ok || len(errs) == 0 {
			t.Fatalf("want errors in the imported event, got %v", p)
		}
		after, _ := p["after"].(map[string]any)
		if after["documentType"] != "Releve de compte" {
			t.Fatalf("recognized fields must be kept: %v", after)
		}
	})

	// 5. Correction (US-22): a field PATCH through the API re-files the
	// document, sets status validated and appends the corrected event.
	t.Run("correction: a field PATCH re-files and validates", func(t *testing.T) {
		pending := listRecords(t, "documents", "(status='pending')", "")
		if len(pending) != 1 {
			t.Fatalf("want 1 pending document, got %d", len(pending))
		}
		id := pending[0]["id"].(string)
		status, out := call(t, http.MethodPatch, "/api/collections/documents/records/"+id,
			map[string]any{"emissionDate": "2026-01-15"})
		if status != 200 {
			t.Fatalf("correction refused: %d %v", status, out)
		}
		want := "files/BRED/2026-01_Releve de compte_Thomas.pdf"
		doc := oneRecord(t, "documents", "(id='"+id+"')")
		if doc["status"] != "validated" {
			t.Fatalf("status: %v", doc["status"])
		}
		if doc["file_key"] != want {
			t.Fatalf("file_key: %v, want %v", doc["file_key"], want)
		}
		if !s3Exists(t, want) || s3Exists(t, "pending/no-date.pdf") {
			t.Fatal("S3 state does not match the correction")
		}
		p := eventPayloadOfType(t, eventsOf(t, id), "corrected")
		before, _ := p["before"].(map[string]any)
		after, _ := p["after"].(map[string]any)
		if before["file_key"] != "pending/no-date.pdf" || before["status"] != "pending" {
			t.Fatalf("before: %v", before)
		}
		if after["file_key"] != want || after["status"] != "validated" {
			t.Fatalf("after: %v", after)
		}
	})

	// 6. Validation (US-23): a status-only PATCH appends the validated
	// event; file_key is untouched and the snapshots carry the status
	// alone.
	t.Run("validation: a status-only PATCH appends validated", func(t *testing.T) {
		hash := putPDF(t, "inbox/statement-2.pdf",
			"BRED", "Releve de compte", "Date : 15/01/2026", "Compte Thomas")
		poll(t)
		poll(t)
		doc := oneRecord(t, "documents", "(sha256='"+hash+"')")
		if doc["status"] != "processed" {
			t.Fatalf("status: %v", doc["status"])
		}
		id := doc["id"].(string)
		oldKey := doc["file_key"].(string)
		status, out := call(t, http.MethodPatch, "/api/collections/documents/records/"+id,
			map[string]any{"status": "validated"})
		if status != 200 {
			t.Fatalf("validation refused: %d %v", status, out)
		}
		doc = oneRecord(t, "documents", "(id='"+id+"')")
		if doc["file_key"] != oldKey {
			t.Fatalf("file_key must not change: %v -> %v", oldKey, doc["file_key"])
		}
		p := eventPayloadOfType(t, eventsOf(t, id), "validated")
		before, _ := p["before"].(map[string]any)
		after, _ := p["after"].(map[string]any)
		if before["status"] != "processed" || after["status"] != "validated" {
			t.Fatalf("snapshots: %v -> %v", before, after)
		}
		if _, has := before["file_key"]; has {
			t.Fatal("validated snapshots carry the status alone")
		}
	})

	// 7. Append-only (US-04): PATCH and DELETE on document_events are
	// refused, superuser included, and the count is unchanged.
	t.Run("append-only: document_events mutations are refused", func(t *testing.T) {
		evs := listRecords(t, "document_events", "", "")
		if len(evs) == 0 {
			t.Fatal("no events to protect")
		}
		id := evs[0]["id"].(string)
		if status, _ := call(t, http.MethodPatch, "/api/collections/document_events/records/"+id,
			map[string]any{"event_type": "corrected"}); status < 400 {
			t.Fatalf("PATCH on document_events must be refused, got %d", status)
		}
		if status, _ := call(t, http.MethodDelete, "/api/collections/document_events/records/"+id, nil); status < 400 {
			t.Fatalf("DELETE on document_events must be refused, got %d", status)
		}
		if n := len(listRecords(t, "document_events", "", "")); n != len(evs) {
			t.Fatalf("event count changed: %d -> %d", len(evs), n)
		}
	})

	// 8. Recovery (US-11): a stuck record - file_key still under inbox/,
	// created directly through the API - is completed by the next pass.
	t.Run("recovery: a stuck filing is completed by the next pass", func(t *testing.T) {
		hash := putPDF(t, "inbox/stuck.pdf",
			"BRED", "Releve de compte", "Date : 01/02/2026", "Compte Thomas")
		status, out := call(t, http.MethodPost, "/api/collections/documents/records", map[string]any{
			"sha256":          hash,
			"file_key":        "inbox/stuck.pdf",
			"status":          "processed",
			"template":        "bank_statement",
			"emittingCompany": "BRED",
			"documentType":    "Releve de compte",
			"emissionDate":    "2026-02-01",
			"bankAccount":     "Thomas",
		})
		if status != 200 {
			t.Fatalf("stuck record creation: %d %v", status, out)
		}
		poll(t) // the recovery runs at the start of the pass
		want := "files/BRED/2026-02_Releve de compte_Thomas.pdf"
		doc := oneRecord(t, "documents", "(sha256='"+hash+"')")
		if doc["file_key"] != want {
			t.Fatalf("file_key: %v, want %v", doc["file_key"], want)
		}
		if !s3Exists(t, want) || s3Exists(t, "inbox/stuck.pdf") {
			t.Fatal("recovery did not complete the filing")
		}
	})

	// 9. Restart: the container restarts with all state intact.
	t.Run("restart: state survives a container restart", func(t *testing.T) {
		before := listRecords(t, "documents", "", "")
		if out, err := run("docker", "restart", appC); err != nil {
			t.Fatalf("docker restart: %v\n%s", err, out)
		}
		if err := retry(60*time.Second, checkHealth); err != nil {
			t.Fatalf("app never came back: %v", err)
		}
		if err := authenticate(); err != nil {
			t.Fatalf("superuser reauthentication after restart: %v", err)
		}
		after := listRecords(t, "documents", "", "")
		if len(after) != len(before) {
			t.Fatalf("documents lost across restart: %d -> %d", len(before), len(after))
		}
	})
}
