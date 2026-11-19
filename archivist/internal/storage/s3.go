// Package storage wraps the MinIO S3 client (US-01, US-10..US-17).
package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"path"
	"regexp"
	"strconv"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type Client struct {
	mc     *minio.Client
	Bucket string
	// prefixes are the bucket prefixes the application organizes itself
	// around (inbox, pending, files); read them through Prefixes().
	prefixes       Prefixes
	StartupWarning string
}

func New(endpoint, accessKey, secretKey, bucket, region string, useSSL bool, prefixes Prefixes) (*Client, error) {
	mc, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
		Region: region,
	})
	if err != nil {
		return nil, err
	}
	c := &Client{mc: mc, Bucket: bucket, prefixes: prefixes}

	ctx := context.Background()
	exists, err := mc.BucketExists(ctx, bucket)
	if err != nil {
		return nil, fmt.Errorf("bucket check: %w", err)
	}
	if !exists {
		return nil, fmt.Errorf("bucket %q does not exist", bucket)
	}
	// Versioning is not required, but recommended: without it, any deletion
	// performed by the application (or by a bug) is final; with it, delete
	// markers keep deleted objects recoverable.
	if vc, err := mc.GetBucketVersioning(ctx, bucket); err != nil {
		c.StartupWarning = fmt.Sprintf("could not check bucket versioning: %v", err)
	} else if vc.Status != "Enabled" {
		c.StartupWarning = "bucket has versioning disabled; consider enabling it to reduce the risk of losing a file to a bug"
	}
	return c, nil
}

// Prefixes are the three bucket prefixes the application organizes itself
// around. Files may be empty when documents are filed at the bucket root.
type Prefixes struct {
	Inbox   string
	Pending string
	Files   string
}

// DefaultPrefixes matches the historical bucket layout.
var DefaultPrefixes = Prefixes{Inbox: "inbox/", Pending: "pending/", Files: "files/"}

// NormalizePrefix trims the surrounding slashes and appends the trailing
// one; "" and "/" both map to the bucket root.
func NormalizePrefix(value string) string {
	v := strings.Trim(value, "/")
	if v == "" {
		return ""
	}
	return v + "/"
}

// Prefixes returns the bucket prefixes the client was built with.
func (c *Client) Prefixes() Prefixes {
	return c.prefixes
}

func (p Prefixes) under(key, prefix string) bool {
	if prefix == "" {
		return true
	}
	return strings.HasPrefix(key, prefix)
}

// IsReconciliationKey reports whether the key belongs to the reconciliation
// scan set (US-41): a PDF under pending or files — under the whole bucket
// when files is the root — and never under inbox (the poller's business).
func (p Prefixes) IsReconciliationKey(key string) bool {
	if !strings.EqualFold(path.Ext(key), ".pdf") {
		return false
	}
	if p.under(key, p.Inbox) {
		return false
	}
	return p.under(key, p.Pending) || p.under(key, p.Files)
}

// InboxObject is one listed S3 object.
type InboxObject struct {
	Key  string
	Size int64
	ETag string
}

// ListReconciliationPDFs lists PDFs recursively under pending/ and files/ —
// the whole bucket when files is the root — never under inbox/ (US-41).
func (c *Client) ListReconciliationPDFs(ctx context.Context) ([]InboxObject, error) {
	var out []InboxObject
	list := []string{c.prefixes.Pending, c.prefixes.Files}
	if c.prefixes.Files == "" {
		list = []string{""}
	}
	seen := map[string]bool{}
	for _, prefix := range list {
		for obj := range c.mc.ListObjects(ctx, c.Bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true}) {
			if obj.Err != nil {
				return nil, obj.Err
			}
			if strings.HasSuffix(obj.Key, "/") || seen[obj.Key] || !c.prefixes.IsReconciliationKey(obj.Key) {
				continue
			}
			seen[obj.Key] = true
			out = append(out, InboxObject{Key: obj.Key, Size: obj.Size, ETag: obj.ETag})
		}
	}
	return out, nil
}

// Hash streams the object and computes its sha256 without holding the
// content in memory: the consistency tools hash every object of the bucket
// and never need the bytes themselves.
func (c *Client) Hash(ctx context.Context, key string) (string, error) {
	obj, err := c.mc.GetObject(ctx, c.Bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return "", err
	}
	defer obj.Close()
	h := sha256.New()
	if _, err := io.Copy(h, obj); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// ListInbox lists the objects under the inbox prefix (US-10).
func (c *Client) ListInbox(ctx context.Context) ([]InboxObject, error) {
	var out []InboxObject
	for obj := range c.mc.ListObjects(ctx, c.Bucket, minio.ListObjectsOptions{Prefix: c.prefixes.Inbox, Recursive: true}) {
		if obj.Err != nil {
			return nil, obj.Err
		}
		if strings.HasSuffix(obj.Key, "/") {
			continue
		}
		out = append(out, InboxObject{Key: obj.Key, Size: obj.Size, ETag: obj.ETag})
	}
	return out, nil
}

// Get downloads the object into memory and computes its sha256 on the fly
// (US-12: streamed in memory, no disk writes).
func (c *Client) Get(ctx context.Context, key string) ([]byte, string, error) {
	data, err := c.getRaw(ctx, key)
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(data)
	return data, hex.EncodeToString(sum[:]), nil
}

func (c *Client) getRaw(ctx context.Context, key string) ([]byte, error) {
	obj, err := c.mc.GetObject(ctx, c.Bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	defer obj.Close()
	return io.ReadAll(obj)
}

func (c *Client) Exists(ctx context.Context, key string) bool {
	_, err := c.mc.StatObject(ctx, c.Bucket, key, minio.StatObjectOptions{})
	return err == nil
}

func (c *Client) Copy(ctx context.Context, srcKey, dstKey string) error {
	_, err := c.mc.CopyObject(ctx,
		minio.CopyDestOptions{Bucket: c.Bucket, Object: dstKey},
		minio.CopySrcOptions{Bucket: c.Bucket, Object: srcKey})
	return err
}

// Remove deletes the object. On a versioned bucket this creates a delete
// marker and the data remains recoverable; without versioning the deletion
// is final (see the startup warning in New).
func (c *Client) Remove(ctx context.Context, key string) error {
	return c.mc.RemoveObject(ctx, c.Bucket, key, minio.RemoveObjectOptions{})
}

var extRe = regexp.MustCompile(`(\.[^./]+)$`)

// ResolveConflict returns desired when no object exists under that key,
// otherwise the name with an incremental numeric suffix before the extension
// (US-15, US-17): "name.pdf" -> "name (1).pdf", "name (2).pdf", ...
func (c *Client) ResolveConflict(ctx context.Context, desired string) (string, error) {
	if !c.Exists(ctx, desired) {
		return desired, nil
	}
	ext := extRe.FindString(desired)
	base := strings.TrimSuffix(desired, ext)
	for n := 1; n < 1000; n++ {
		candidate := base + " (" + strconv.Itoa(n) + ")" + ext
		if !c.Exists(ctx, candidate) {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("cannot resolve conflict on %q", desired)
}
