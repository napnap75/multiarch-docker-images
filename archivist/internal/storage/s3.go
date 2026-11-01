// Package storage wraps the MinIO S3 client (US-01, US-10..US-17).
package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type Client struct {
	mc     *minio.Client
	Bucket string
}

func New(endpoint, accessKey, secretKey, bucket string, useSSL bool) (*Client, error) {
	mc, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
	})
	if err != nil {
		return nil, err
	}
	c := &Client{mc: mc, Bucket: bucket}

	ctx := context.Background()
	exists, err := mc.BucketExists(ctx, bucket)
	if err != nil {
		return nil, fmt.Errorf("bucket check: %w", err)
	}
	if !exists {
		return nil, fmt.Errorf("bucket %q does not exist", bucket)
	}
	// US-01: the bucket must be versioned (delete markers keep data recoverable).
	vc, err := mc.GetBucketVersioning(ctx, bucket)
	if err != nil {
		return nil, fmt.Errorf("versioning check: %w", err)
	}
	if vc.Status != "Enabled" {
		return nil, fmt.Errorf("bucket %q is not versioned (status %q)", bucket, vc.Status)
	}
	return c, nil
}

// InboxObject is one object listed under inbox/.
type InboxObject struct {
	Key  string
	Size int64
	ETag string
}

// ListInbox lists the objects under the inbox/ prefix (US-10).
func (c *Client) ListInbox(ctx context.Context) ([]InboxObject, error) {
	var out []InboxObject
	for obj := range c.mc.ListObjects(ctx, c.Bucket, minio.ListObjectsOptions{Prefix: "inbox/", Recursive: true}) {
		if obj.Err != nil {
			return nil, obj.Err
		}
		out = append(out, InboxObject{Key: obj.Key, Size: obj.Size, ETag: obj.ETag})
	}
	return out, nil
}

// Get downloads the object into memory and computes its sha256 on the fly
// (US-12: streamed in memory, no disk writes).
func (c *Client) Get(ctx context.Context, key string) ([]byte, string, error) {
	obj, err := c.mc.GetObject(ctx, c.Bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, "", err
	}
	defer obj.Close()
	data, err := io.ReadAll(obj)
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(data)
	return data, hex.EncodeToString(sum[:]), nil
}

func (c *Client) Exists(ctx context.Context, key string) bool {
	_, err := c.mc.StatObject(ctx, c.Bucket, key, minio.StatObjectOptions{})
	return err == nil
}

func (c *Client) Copy(ctx context.Context, srcKey, dstKey string) error {
	_, err := c.mc.CopyObject(ctx,
		minio.CopyDestOptions{Bucket: c.Bucket, Object: dstKey},
		minio.CopySourceOptions{Bucket: c.Bucket, Object: srcKey})
	return err
}

// Remove deletes the object; with a versioned bucket this creates a delete
// marker and the data remains recoverable.
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
