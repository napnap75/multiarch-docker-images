#!/usr/bin/env python3
# Import pre-existing filed documents into the Archivist PocketBase database,
# bypassing the import pipeline.
#
# What this script does:
#   1. Walks the "files/" prefix of the configured S3 bucket and finds every
#      object ending in ".meta.json". Each of these sidecar files describes a
#      PDF document filed by a previous tool; the PDF itself sits next to it
#      under the key found in the "current_key" metadata field.
#   2. For each meta file, maps its fields onto the Archivist "documents"
#      collection (mapping configurable in FIELD_MAPPING below), using the
#      metadata sha256 as the document identity.
#   3. Skips documents whose sha256 already exists in the database
#      (deduplication, the unique index on documents.sha256 would reject them
#      anyway).
#   4. Creates the "documents" record (status is always "processed"; the
#      extracted_text is stored as a single-element "pages" array) and appends
#      one "imported" event to "document_events", using the internal Archivist
#      payload format (before/after snapshots).
#   5. Never deletes or moves anything on S3: the PDFs and their .meta.json
#      sidecars are left untouched.
#
# Requirements:
#   pip install boto3 requests
#
# Usage:
#   python3 import_meta_files.py            # normal run
#   python3 import_meta_files.py --dry-run # list what would be imported, no write

import argparse
import datetime
import json
import sys

import boto3
import requests
from botocore.config import Config

# ---------------------------------------------------------------------------
# Hardcoded configuration -- edit this section before running.
# ---------------------------------------------------------------------------

S3_ENDPOINT = "s3.mydomain.fr:9000"
S3_ACCESS_KEY = "..."
S3_SECRET_KEY = "..."
S3_BUCKET = "documents"
S3_USE_SSL = True
S3_REGION = "garage"
S3_PREFIX = "files/"

POCKETBASE_URL = "http://127.0.0.1:8090"
POCKETBASE_ADMIN_EMAIL = "me@mydomain.fr"
POCKETBASE_ADMIN_PASSWORD = "..."

# Mapping from a ".meta.json" key (dot path allowed for nested values) to a
# column of the "documents" collection. The fixed columns below are always
# populated and must not appear here: sha256, file_key, pages, status.
# Value syntax: "key" or "key1|key2" (first non-empty wins).
FIELD_MAPPING = {
    "emitting_company": "emittingCompany",
    "document_type": "documentType",
    "emission_date": "emissionDate",
    "period": "period",
    "optional_fields.appartement": "appartement",
}

# ---------------------------------------------------------------------------
# End of configuration.
# ---------------------------------------------------------------------------

META_SUFFIX = ".meta.json"


def s3_client():
    scheme = "https" if S3_USE_SSL else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{S3_ENDPOINT}",
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def list_meta_objects(s3):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(META_SUFFIX):
                yield obj["Key"]


def load_meta(s3, key):
    body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
    return json.loads(body.decode("utf-8"))


def resolve_path(meta, path):
    # "a.b" resolves into nested dicts; "a|b" returns the first non-empty hit.
    for alternative in path.split("|"):
        value = meta
        ok = True
        for part in alternative.strip().split("."):
            if not isinstance(value, dict) or part not in value:
                ok = False
                break
            value = value[part]
        if ok and value not in (None, ""):
            return value
    return None


class PocketBase:
    def __init__(self):
        self.base = POCKETBASE_URL.rstrip("/")
        self.token = None

    def login(self):
        r = requests.post(
            f"{self.base}/api/collections/_superusers/auth-with-password",
            json={"identity": POCKETBASE_ADMIN_EMAIL, "password": POCKETBASE_ADMIN_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        self.token = r.json()["token"]

    def _headers(self):
        return {"Authorization": self.token}

    def find_document_by_sha256(self, sha256):
        r = requests.get(
            f"{self.base}/api/collections/documents/records",
            params={"filter": f'sha256 = "{sha256}"', "perPage": 1},
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return items[0] if items else None

    def create_record(self, collection, data):
        r = requests.post(
            f"{self.base}/api/collections/{collection}/records",
            json=data,
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


def build_document_record(meta):
    record = {
        "sha256": meta.get("sha256"),
        "file_key": meta.get("current_key"),
        "status": "processed",
        "pages": [meta["extracted_text"]] if meta.get("extracted_text") else [],
    }
    if not record["sha256"] or not record["file_key"]:
        raise ValueError("meta file is missing sha256 or current_key")
    for meta_path, column in FIELD_MAPPING.items():
        value = resolve_path(meta, meta_path)
        if value not in (None, ""):
            record[column] = value
    return record


def build_imported_event(meta, fields):
    # Internal Archivist payload format: a self-contained event with
    # before/after snapshots. The document was already under its final
    # key, so "before" carries the original file name from the previous
    # tool, "after" carries the current key, the status and the fields.
    timestamp = meta.get("created_at")
    if not timestamp:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    after = {"file_key": meta.get("current_key"), "status": "processed"}
    after.update(fields)
    payload = {
        "event_type": "imported",
        "timestamp": timestamp,
        "before": {"file_key": meta.get("original_key", "")},
        "after": after,
    }
    return {
        "event_type": "imported",
        "payload": json.dumps(payload),
        "timestamp": timestamp,
    }


def main():
    parser = argparse.ArgumentParser(description="Import existing filed documents into Archivist")
    parser.add_argument("--dry-run", action="store_true", help="list what would be imported, write nothing")
    args = parser.parse_args()

    s3 = s3_client()
    pb = PocketBase()
    pb.login()

    created = skipped = failed = 0
    for key in list_meta_objects(s3):
        try:
            meta = load_meta(s3, key)
            doc = build_document_record(meta)
        except Exception as exc:
            print(f"ERROR  {key}: unreadable meta file: {exc}", file=sys.stderr)
            failed += 1
            continue

        existing = pb.find_document_by_sha256(doc["sha256"])
        if existing:
            print(f"SKIP   {key}: sha256 already in database (document {existing['id']})")
            skipped += 1
            continue

        fields = {c: v for c, v in doc.items() if c not in ("sha256", "file_key", "status", "pages")}
        if args.dry_run:
            print(f"WOULD IMPORT {key} -> {doc['file_key']} ({doc['sha256'][:12]}...)")
            created += 1
            continue

        try:
            record = pb.create_record("documents", doc)
            event = build_imported_event(meta, fields)
            event["document"] = record["id"]
            pb.create_record("document_events", event)
            print(f"OK     {key} -> document {record['id']}")
            created += 1
        except requests.HTTPError as exc:
            print(f"ERROR  {key}: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
            failed += 1

    print(f"Done: {created} created, {skipped} skipped (duplicates), {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
