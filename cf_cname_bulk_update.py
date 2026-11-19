#!/usr/bin/env python3
"""Bulk-update Cloudflare DNS CNAME records for one zone, with a before/after review.

Standard library only (Python 3.8+). Workflow:

    export   Pull the zone's current CNAMEs into a CSV.
    plan     Compare an edited CSV (or BIND file) with live DNS and show before/after. Read-only.
    apply    Same comparison, then ask for confirmation, back up, push, and verify.
    rollback Restore records from a backup file written by `apply`.

Auth: set CLOUDFLARE_API_TOKEN (needs Zone > DNS > Edit on the target zone only).
"""

import argparse
import csv
import datetime
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE_DEFAULT = "https://api.cloudflare.com/client/v4"
CSV_COLUMNS = ["id", "name", "content", "ttl", "proxied", "comment"]
EDITABLE_FIELDS = ("content", "ttl", "proxied", "comment")
BATCH_SIZE = 100
MAX_ATTEMPTS = 5


class CloudflareError(Exception):
    pass


class InputError(Exception):
    pass


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, token, api_base=API_BASE_DEFAULT, ca_bundle=None):
        self.token = token
        self.api_base = api_base.rstrip("/")
        # urllib honors HTTPS_PROXY / NO_PROXY from the environment automatically.
        self.ssl_context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None

    def request(self, method, path, params=None, body=None):
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "User-Agent": "cf-cname-bulk-update/1.0",
        }
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, context=self.ssl_context, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 429 or exc.code >= 500:
                    wait = float(exc.headers.get("Retry-After") or 2 ** attempt)
                    last_error = CloudflareError("HTTP %d from %s %s" % (exc.code, method, path))
                    if attempt < MAX_ATTEMPTS:
                        print("  HTTP %d, retrying in %.0fs..." % (exc.code, wait), file=sys.stderr)
                        time.sleep(wait)
                        continue
                    raise last_error
                raise CloudflareError("HTTP %d from %s %s: %s" % (exc.code, method, path, describe_errors(raw)))
            except urllib.error.URLError as exc:
                raise CloudflareError("Could not reach %s: %s" % (self.api_base, exc.reason))
            if not payload.get("success", False):
                raise CloudflareError("%s %s failed: %s" % (method, path, describe_errors(json.dumps(payload))))
            return payload
        raise last_error or CloudflareError("request failed")


def describe_errors(raw):
    try:
        errors = json.loads(raw).get("errors") or []
        if errors:
            return "; ".join("%s (code %s)" % (e.get("message"), e.get("code")) for e in errors)
    except (ValueError, AttributeError):
        pass
    return raw[:300]


def resolve_zone(client, zone_name, zone_id):
    """Return (zone_id, zone_name), looking up whichever one was not supplied."""
    if zone_id:
        result = client.request("GET", "/zones/" + zone_id)["result"]
        return result["id"], result["name"].lower()
    if not zone_name:
        raise InputError("Provide --zone (name) or --zone-id.")
    result = client.request("GET", "/zones", params={"name": zone_name})["result"]
    if not result:
        raise InputError("Zone %r not found or the token cannot see it." % zone_name)
    return result[0]["id"], result[0]["name"].lower()


def list_cnames(client, zone_id):
    records, page = [], 1
    while True:
        payload = client.request(
            "GET", "/zones/%s/dns_records" % zone_id,
            params={"type": "CNAME", "page": page, "per_page": 100},
        )
        records.extend(payload["result"])
        total_pages = (payload.get("result_info") or {}).get("total_pages", 1)
        if page >= total_pages:
            return records
        page += 1


# ---------------------------------------------------------------------------
# Normalizing and parsing input
# ---------------------------------------------------------------------------

def normalize_host(value):
    return value.strip().lower().rstrip(".")


def qualify_name(name, zone):
    name = normalize_host(name)
    if name in ("@", ""):
        return zone
    if name == zone or name.endswith("." + zone):
        return name
    return name + "." + zone


def parse_ttl(value):
    try:
        ttl = int(str(value).strip().lower().replace("auto", "1"))
    except ValueError:
        raise InputError("Invalid TTL %r (use a number of seconds, or 1 / auto)." % value)
    if ttl != 1 and not 30 <= ttl <= 86400:
        raise InputError("TTL %d out of range (1 for Auto, otherwise 30-86400)." % ttl)
    return ttl


def parse_bool(value):
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0"):
        return False
    raise InputError("Invalid proxied value %r (use true or false)." % value)


def read_csv(path, zone):
    """Rows of desired state. A blank cell means 'leave this field unchanged'."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]
        if "name" not in headers:
            raise InputError("CSV needs at least a 'name' column. Found: %s" % ", ".join(headers))
        unknown = [h for h in headers if h not in CSV_COLUMNS]
        if unknown:
            raise InputError("Unknown CSV column(s): %s" % ", ".join(unknown))
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            try:
                rows.append(build_desired(row, zone))
            except InputError as exc:
                raise InputError("%s line %d: %s" % (path, line_no, exc))
    return rows


def build_desired(row, zone):
    if not row.get("name"):
        raise InputError("missing name")
    return {
        "id": row.get("id") or None,
        "name": qualify_name(row["name"], zone),
        "content": normalize_host(row["content"]) if row.get("content") else None,
        "ttl": parse_ttl(row["ttl"]) if row.get("ttl") else None,
        "proxied": parse_bool(row["proxied"]) if row.get("proxied") else None,
        "comment": row.get("comment") or None,
    }


def parse_bind(path, zone):
    """Minimal BIND reader: CNAME lines only, plus $ORIGIN and $TTL.

    Also understands the Cloudflare export hint '; cf_tags=cf-proxied:true'.
    Everything that is not a CNAME is skipped.
    """
    origin, default_ttl, last_name = zone, None, zone
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            code, _, comment = raw.partition(";")
            if not code.strip():
                continue
            tokens = code.split()
            if tokens[0].upper() == "$ORIGIN":
                origin = normalize_host(tokens[1])
                continue
            if tokens[0].upper() == "$TTL":
                default_ttl = parse_ttl(tokens[1])
                continue
            if tokens[0].startswith("$"):
                continue
            name = last_name if code[0].isspace() else tokens.pop(0)
            tokens = [t for t in tokens if t.upper() != "IN"]
            ttl = None
            if tokens and tokens[0][0].isdigit():
                ttl = parse_ttl(tokens.pop(0))
            if len(tokens) < 2 or tokens[0].upper() != "CNAME":
                continue
            target = tokens[1]
            if name.endswith("."):
                fqdn = normalize_host(name)
            else:
                fqdn = qualify_name(name, origin)
            last_name = name
            target = normalize_host(target) if target.endswith(".") else qualify_name(target, origin)
            proxied = None
            match = re.search(r"cf-proxied:(true|false)", comment)
            if match:
                proxied = match.group(1) == "true"
            rows.append({
                "id": None, "name": fqdn, "content": target,
                "ttl": ttl if ttl is not None else default_ttl,
                "proxied": proxied, "comment": None,
            })
    return rows


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def current_value(record, field):
    if field == "comment":
        return record.get("comment") or ""
    if field == "content":
        return normalize_host(record.get("content", ""))
    if field == "proxied":
        return bool(record.get("proxied"))
    return record.get(field)


def build_plan(current_records, desired_rows):
    """Return (changes, unchanged_count, errors).

    Each change: {id, name, before: {...}, after: {...}, notes: [...]} listing changed fields only.
    """
    by_id = {r["id"]: r for r in current_records}
    by_name = {}
    for rec in current_records:
        by_name.setdefault(normalize_host(rec["name"]), []).append(rec)

    changes, errors, unchanged, seen = [], [], 0, set()
    for row in desired_rows:
        if row["id"]:
            record = by_id.get(row["id"])
            if record is None:
                errors.append("%s: id %s is not a CNAME in this zone" % (row["name"], row["id"]))
                continue
            if normalize_host(record["name"]) != row["name"]:
                errors.append("%s: id %s belongs to %s, not this name" % (row["name"], row["id"], record["name"]))
                continue
        else:
            matches = by_name.get(row["name"], [])
            if not matches:
                errors.append("%s: no existing CNAME with this name" % row["name"])
                continue
            if len(matches) > 1:
                errors.append("%s: %d CNAMEs share this name, include the id column" % (row["name"], len(matches)))
                continue
            record = matches[0]
        if record["id"] in seen:
            errors.append("%s: listed more than once" % row["name"])
            continue
        seen.add(record["id"])

        before, after, notes = {}, {}, []
        for field in EDITABLE_FIELDS:
            wanted = row[field]
            if wanted is None or wanted == current_value(record, field):
                continue
            before[field] = current_value(record, field)
            after[field] = wanted

        final_proxied = after.get("proxied", current_value(record, "proxied"))
        final_ttl = after.get("ttl", current_value(record, "ttl"))
        if final_proxied and final_ttl != 1:
            if "ttl" in after:
                errors.append("%s: proxied records must use TTL Auto (1), got %s" % (row["name"], final_ttl))
                continue
            before["ttl"], after["ttl"] = current_value(record, "ttl"), 1
            notes.append("TTL forced to Auto because the record is proxied")

        if not after:
            unchanged += 1
            continue
        changes.append({"id": record["id"], "name": normalize_host(record["name"]),
                        "before": before, "after": after, "notes": notes,
                        "snapshot": {f: current_value(record, f) for f in EDITABLE_FIELDS}})
    return changes, unchanged, errors


def fmt(value):
    if value == "":
        return "(empty)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return "Auto" if value == 1 and not isinstance(value, bool) else str(value)


def print_plan(zone, changes, unchanged, errors, total_rows):
    print("\nZone: %s" % zone)
    print("Rows read: %d | will change: %d | already match: %d | problems: %d\n"
          % (total_rows, len(changes), unchanged, len(errors)))
    for change in changes:
        print("  %s" % change["name"])
        for field in change["after"]:
            old, new = fmt(change["before"][field]), fmt(change["after"][field])
            print("      %-8s %s  ->  %s" % (field, old, new))
        for note in change["notes"]:
            print("      note: %s" % note)
    if errors:
        print("\nProblems (these must be fixed before apply):")
        for message in errors:
            print("  ! %s" % message)
    print()


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def patch_body(change):
    body = {"id": change["id"]}
    body.update(change["after"])
    return body


def push_changes(client, zone_id, changes, use_batch=True):
    if use_batch:
        for start in range(0, len(changes), BATCH_SIZE):
            chunk = changes[start:start + BATCH_SIZE]
            client.request("POST", "/zones/%s/dns_records/batch" % zone_id,
                           body={"patches": [patch_body(c) for c in chunk]})
            print("  pushed %d/%d" % (min(start + BATCH_SIZE, len(changes)), len(changes)))
    else:
        for index, change in enumerate(changes, start=1):
            body = dict(change["after"])
            client.request("PATCH", "/zones/%s/dns_records/%s" % (zone_id, change["id"]), body=body)
            print("  pushed %d/%d" % (index, len(changes)))


def verify(client, zone_id, changes):
    live = {r["id"]: r for r in list_cnames(client, zone_id)}
    failures = []
    for change in changes:
        record = live.get(change["id"])
        if record is None:
            failures.append("%s: record no longer exists" % change["name"])
            continue
        for field, wanted in change["after"].items():
            actual = current_value(record, field)
            if actual != wanted:
                failures.append("%s: %s is %s, expected %s" % (change["name"], field, fmt(actual), fmt(wanted)))
    return failures


def write_backup(zone, changes, directory):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, "backup-%s-%s.json" % (zone, stamp))
    os.makedirs(directory, exist_ok=True)
    records = [dict(id=c["id"], name=c["name"], **c["snapshot"]) for c in changes]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"zone": zone, "created": stamp, "records": records}, handle, indent=2)
    return path


def confirm(zone, assume_yes):
    if assume_yes:
        return True
    answer = input("Type the zone name (%s) to confirm, anything else cancels: " % zone)
    return answer.strip().lower() == zone


def run_plan_and_apply(client, zone_id, zone, desired, apply_changes, args):
    current = list_cnames(client, zone_id)
    changes, unchanged, errors = build_plan(current, desired)
    print_plan(zone, changes, unchanged, errors, len(desired))
    if errors:
        return 1
    if not changes:
        print("Nothing to change.")
        return 0
    if not apply_changes:
        print("Dry run only. Re-run with the `apply` command to push these changes.")
        return 0
    if not confirm(zone, args.yes):
        print("Cancelled. No changes were made.")
        return 1
    backup_path = write_backup(zone, changes, args.backup_dir)
    print("Backup written: %s" % backup_path)
    push_changes(client, zone_id, changes, use_batch=not args.no_batch)
    failures = verify(client, zone_id, changes)
    if failures:
        print("\nVERIFY FAILED:")
        for message in failures:
            print("  ! %s" % message)
        print("Restore with: rollback --backup %s" % backup_path)
        return 2
    print("\nVerified %d record(s) match the plan. Roll back with: rollback --backup %s"
          % (len(changes), backup_path))
    return 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_export(client, zone_id, zone, args):
    records = list_cnames(client, zone_id)
    if args.name_contains:
        records = [r for r in records if args.name_contains.lower() in r["name"].lower()]
    records.sort(key=lambda r: r["name"])
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for rec in records:
            writer.writerow([rec["id"], normalize_host(rec["name"]), normalize_host(rec["content"]),
                             rec.get("ttl"), "true" if rec.get("proxied") else "false",
                             rec.get("comment") or ""])
    print("Exported %d CNAME record(s) from %s to %s" % (len(records), zone, args.out))
    print("Edit the content/ttl/proxied/comment cells you want to change and delete rows you do not.")
    return 0


def load_desired(args, zone):
    if args.csv:
        return read_csv(args.csv, zone)
    return parse_bind(args.bind, zone)


def cmd_plan(client, zone_id, zone, args):
    return run_plan_and_apply(client, zone_id, zone, load_desired(args, zone), False, args)


def cmd_apply(client, zone_id, zone, args):
    return run_plan_and_apply(client, zone_id, zone, load_desired(args, zone), True, args)


def cmd_rollback(client, zone_id, zone, args):
    with open(args.backup, encoding="utf-8") as handle:
        backup = json.load(handle)
    if backup["zone"] != zone:
        raise InputError("Backup is for zone %s but you selected %s." % (backup["zone"], zone))
    desired = [{"id": r["id"], "name": r["name"], "content": r["content"], "ttl": r["ttl"],
                "proxied": r["proxied"], "comment": r["comment"]} for r in backup["records"]]
    print("Rolling back %d record(s) from %s" % (len(desired), args.backup))
    return run_plan_and_apply(client, zone_id, zone, desired, True, args)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zone", help="Zone name, e.g. example.com")
    parser.add_argument("--zone-id", help="Zone ID (skips the name lookup)")
    parser.add_argument("--api-base", default=os.environ.get("CLOUDFLARE_API_BASE", API_BASE_DEFAULT),
                        help=argparse.SUPPRESS)
    parser.add_argument("--ca-bundle", default=os.environ.get("SSL_CERT_FILE"),
                        help="PEM CA bundle, for networks that inspect TLS")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Write the zone's CNAMEs to a CSV")
    export.add_argument("--out", default="cnames.csv")
    export.add_argument("--name-contains", help="Only export names containing this text")
    export.set_defaults(func=cmd_export)

    for name, func, help_text in (("plan", cmd_plan, "Show before/after, change nothing"),
                                  ("apply", cmd_apply, "Show before/after, confirm, push, verify")):
        cmd = sub.add_parser(name, help=help_text)
        source = cmd.add_mutually_exclusive_group(required=True)
        source.add_argument("--csv", help="Edited CSV from `export`")
        source.add_argument("--bind", help="BIND-format file (CNAME lines only are used)")
        cmd.add_argument("--backup-dir", default="backups")
        cmd.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
        cmd.add_argument("--no-batch", action="store_true", help="PATCH one record at a time")
        cmd.set_defaults(func=func)

    rollback = sub.add_parser("rollback", help="Restore records from a backup file")
    rollback.add_argument("--backup", required=True)
    rollback.add_argument("--backup-dir", default="backups")
    rollback.add_argument("--yes", action="store_true")
    rollback.add_argument("--no-batch", action="store_true")
    rollback.set_defaults(func=cmd_rollback)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        print("Set CLOUDFLARE_API_TOKEN first.", file=sys.stderr)
        return 1
    client = Client(token, args.api_base, args.ca_bundle)
    try:
        zone_id, zone = resolve_zone(client, args.zone and normalize_host(args.zone), args.zone_id)
        return args.func(client, zone_id, zone, args)
    except (InputError, CloudflareError, OSError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
