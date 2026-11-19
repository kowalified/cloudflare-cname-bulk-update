#!/usr/bin/env python3
"""Bulk-update Cloudflare DNS CNAME records for one zone, with a before/after review.

Standard library only (Python 3.8+). Workflow:

    export   Pull the zone's current CNAMEs into a CSV.
    plan     Compare an edited CSV (or BIND file) with live DNS and show before/after. Read-only.
    apply    Same comparison, then ask for confirmation, back up, push, and verify.
    rollback Restore records from a backup file written by `apply`.

Auth: set CLOUDFLARE_API_TOKEN (Zone > DNS > Edit and Zone > Zone > Read, scoped to the zones involved).
With no --zone, records are matched to whichever of the token's zones they belong to.
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


def list_zones(client):
    """Every zone the token can see, as {zone_name: zone_id}."""
    zones, page = {}, 1
    while True:
        payload = client.request("GET", "/zones", params={"page": page, "per_page": 50})
        for zone in payload["result"]:
            zones[zone["name"].lower()] = zone["id"]
        if page >= (payload.get("result_info") or {}).get("total_pages", 1):
            return zones
        page += 1


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
    """Make a name fully qualified. With no zone, names must already be fully qualified."""
    name = normalize_host(name)
    if zone is None:
        if name in ("@", ""):
            raise InputError("'@' needs --zone; otherwise use full names.")
        return name
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


HEADER_ALIASES = {
    "name": ("record", "record name", "hostname", "host", "fqdn", "cname", "domain"),
    "content": ("target", "new target", "new content", "new value", "new cname", "value",
                "destination", "points to"),
}


def map_headers(headers):
    """Map the file's headers to canonical columns.

    Returns (mapping {header: column}, notes). Exact canonical names win over aliases, and any
    column we do not recognize is ignored, so a working spreadsheet can be used as it is.
    """
    cleaned = {h: re.sub(r"[\s_-]+", " ", h.strip().lower()) for h in headers}
    mapping, notes = {}, []
    for column in CSV_COLUMNS:
        exact = [h for h, c in cleaned.items() if c == column]
        aliased = [h for h, c in cleaned.items() if c in HEADER_ALIASES.get(column, ())]
        if exact:
            mapping[exact[0]] = column
        elif len(aliased) == 1:
            mapping[aliased[0]] = column
            notes.append("using column %r as '%s'" % (aliased[0], column))
        elif len(aliased) > 1:
            raise InputError("Several columns could be '%s' (%s). Rename the right one to '%s'."
                             % (column, ", ".join(repr(a) for a in aliased), column))
    ignored = [h for h in headers if h not in mapping]
    if ignored:
        notes.append("ignoring columns: %s" % ", ".join(repr(h) for h in ignored))
    return mapping, notes


def read_csv(path, zone):
    """Rows of desired state. A blank cell means 'leave this field unchanged'."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = [h for h in (reader.fieldnames or []) if h]
        mapping, notes = map_headers(headers)
        if "name" not in mapping.values():
            raise InputError("CSV needs a column with the record names (call it 'name'). Found: %s"
                             % ", ".join(headers))
        for note in notes:
            print("CSV: %s" % note)
        for line_no, raw in enumerate(reader, start=2):
            row = {mapping[h]: (raw.get(h) or "").strip() for h in mapping}
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


def confirm(expected, assume_yes):
    if assume_yes:
        return True
    answer = input("Type %r to confirm, anything else cancels: " % expected)
    return answer.strip().lower() == expected


def assign_zones(desired, zones):
    """Group desired rows by the most specific zone each name belongs to."""
    groups, errors = {}, []
    for row in desired:
        matches = [z for z in zones if row["name"] == z or row["name"].endswith("." + z)]
        if not matches:
            errors.append("%s: not in any zone this token can see" % row["name"])
            continue
        groups.setdefault(max(matches, key=len), []).append(row)
    return groups, errors


def run_plan_and_apply(client, zones, desired, apply_changes, args):
    groups, errors = assign_zones(desired, zones)
    plans = {}
    for zone in sorted(groups):
        current = list_cnames(client, zones[zone])
        changes, unchanged, zone_errors = build_plan(current, groups[zone])
        plans[zone] = changes
        errors.extend(zone_errors)
        print_plan(zone, changes, unchanged, zone_errors, len(groups[zone]))
    unassigned = [e for e in errors if "not in any zone" in e]
    if unassigned:
        print("Names that matched no zone:")
        for message in unassigned:
            print("  ! %s" % message)
        print()
    if errors:
        print("Fix the problems above and try again. Nothing was changed.")
        return 1
    zones_to_change = [z for z in sorted(plans) if plans[z]]
    total = sum(len(plans[z]) for z in zones_to_change)
    if not total:
        print("Nothing to change.")
        return 0
    if len(zones_to_change) > 1:
        print("TOTAL: %d record(s) across %d zones (%s)\n"
              % (total, len(zones_to_change), ", ".join(zones_to_change)))
    if not apply_changes:
        print("Dry run only. Re-run with the `apply` command to push these changes.")
        return 0
    expected = zones_to_change[0] if len(zones_to_change) == 1 else "apply"
    if not confirm(expected, args.yes):
        print("Cancelled. No changes were made.")
        return 1

    exit_code, backups = 0, []
    for zone in zones_to_change:
        changes = plans[zone]
        backup_path = write_backup(zone, changes, args.backup_dir)
        backups.append((zone, backup_path))
        print("[%s] backup written: %s" % (zone, backup_path))
        push_changes(client, zones[zone], changes, use_batch=not args.no_batch)
        failures = verify(client, zones[zone], changes)
        if failures:
            exit_code = 2
            print("[%s] VERIFY FAILED:" % zone)
            for message in failures:
                print("  ! %s" % message)
        else:
            print("[%s] verified %d record(s) match the plan" % (zone, len(changes)))
    print("\nTo undo:")
    for zone, path in backups:
        print("  rollback --backup %s" % path)
    return exit_code


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_export(client, zones, args):
    rows = []
    for zone in sorted(zones):
        rows.extend(list_cnames(client, zones[zone]))
    if args.name_contains:
        rows = [r for r in rows if args.name_contains.lower() in r["name"].lower()]
    rows.sort(key=lambda r: r["name"])
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for rec in rows:
            writer.writerow([rec["id"], normalize_host(rec["name"]), normalize_host(rec["content"]),
                             rec.get("ttl"), "true" if rec.get("proxied") else "false",
                             rec.get("comment") or ""])
    print("Exported %d CNAME record(s) from %d zone(s) to %s" % (len(rows), len(zones), args.out))
    print("Edit the content/ttl/proxied/comment cells you want to change and delete rows you do not.")
    return 0


def load_desired(args):
    if args.csv:
        return read_csv(args.csv, args.zone)
    if not args.zone:
        raise InputError("--bind needs --zone (a BIND file describes one zone).")
    return parse_bind(args.bind, args.zone)


def cmd_plan(client, zones, args):
    return run_plan_and_apply(client, zones, load_desired(args), False, args)


def cmd_apply(client, zones, args):
    return run_plan_and_apply(client, zones, load_desired(args), True, args)


def cmd_rollback(client, zones, args):
    with open(args.backup, encoding="utf-8") as handle:
        backup = json.load(handle)
    if backup["zone"] not in zones:
        raise InputError("Backup is for zone %s, which this token/selection cannot see." % backup["zone"])
    desired = [{"id": r["id"], "name": r["name"], "content": r["content"], "ttl": r["ttl"],
                "proxied": r["proxied"], "comment": r["comment"]} for r in backup["records"]]
    print("Rolling back %d record(s) from %s" % (len(desired), args.backup))
    return run_plan_and_apply(client, {backup["zone"]: zones[backup["zone"]]}, desired, True, args)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zone", help="Limit to one zone, e.g. example.com. Omit to work across every zone the token can see")
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
        if args.zone or args.zone_id:
            zone_id, zone_name = resolve_zone(client, args.zone and normalize_host(args.zone), args.zone_id)
            args.zone = zone_name
            zones = {zone_name: zone_id}
        else:
            zones = list_zones(client)
            if not zones:
                raise InputError("The token cannot see any zones (needs Zone > Zone > Read).")
            print("No --zone given: working across %d zone(s): %s\n" % (len(zones), ", ".join(sorted(zones))))
        return args.func(client, zones, args)
    except (InputError, CloudflareError, OSError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
