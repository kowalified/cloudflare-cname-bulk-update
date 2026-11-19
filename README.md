# Cloudflare CNAME bulk update

Standard-library-only Python (3.8+). No pip install. Works on one zone or across several zones in one run.

This is an example script provided as-is, not an official Cloudflare product. Try it on a test zone first, and review the `plan` output before every `apply`.

## Setup (once per terminal window)

1. **Create an API token** in the Cloudflare dashboard: My Profile > API Tokens > Create Token > Custom token.
   - Permissions: `Zone` > `DNS` > `Edit`, and `Zone` > `Zone` > `Read` (the Zone Read permission is only needed so the script can look up the zone ID from its name; skip it if you always pass `--zone-id`).
   - Zone Resources: `Include` > `Specific zone`, and add every zone you will update (one zone, or several). Without `--zone`, the script works across all zones the token can see.
   - Optional: set an expiry date. Copy the token when it is shown, because it cannot be displayed again.
2. **Set the token in your terminal.** Environment variables only last for the terminal window they were set in, so repeat this whenever you open a new one.
   ```
   read -s CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN
   ```
   Paste the token and press Enter. Nothing is echoed, and the token never lands in your shell history or in a file.
   Windows PowerShell: `$env:CLOUDFLARE_API_TOKEN = Read-Host "Token"`
3. **Check it is set** (prints the length only, not the token):
   ```
   echo ${#CLOUDFLARE_API_TOKEN}
   ```
   A real token is typically 40 characters or more. `0` means it is not set in this window.
4. When you are done: `unset CLOUDFLARE_API_TOKEN`, and delete the token in the dashboard if it was for a one-off job.

## Workflow

```
export  ->  edit the CSV  ->  plan (review)  ->  apply (confirm, backup, push, verify)  ->  rollback if needed
```

**Quickest path, if you already have a spreadsheet of records and their new values:** save it as a CSV with two columns, `name` and `content` (the full record name and its new CNAME target), and skip to step 3.

1. **Export** current CNAMEs (all zones the token can see, or one with `--zone`):
   ```
   python3 cf_cname_bulk_update.py export --out cnames.csv
   python3 cf_cname_bulk_update.py --zone example.com export --out cnames.csv
   ```
2. **Edit** `cnames.csv`. Change `content` (the CNAME target), `ttl`, `proxied`, or `comment`. Delete rows you do not want touched.
   Blank cells mean "leave unchanged". `ttl` accepts seconds (30-86400) or `1`/`auto`. Only `name` is required, so a two-column `name,content` file works.
3. **Plan** (read-only, shows before/after per zone):
   ```
   python3 cf_cname_bulk_update.py plan --csv cnames.csv
   ```
   Use full names (`app.example.com`). Each record is matched to the zone it belongs to. If any name matches no zone or no existing CNAME, nothing is applied.
4. **Apply** (shows the same diff, asks for confirmation, writes a backup per zone, pushes, re-reads to verify):
   ```
   python3 cf_cname_bulk_update.py apply --csv cnames.csv
   ```
   One zone: type the zone name to confirm. Several zones: type `apply`.
5. **Rollback** using a backup file path printed by `apply` (one file per zone):
   ```
   python3 cf_cname_bulk_update.py rollback --backup backups/backup-example.com-<timestamp>.json
   ```

Add `--zone example.com` to any command to limit it to one zone (this also lets the CSV use short names like `app` instead of `app.example.com`).

BIND input works too: `--zone example.com plan --bind zone.txt` (one zone per file). Only CNAME lines are read (`$ORIGIN` and `$TTL` are honored).

## Behavior worth knowing

- Records are matched by `id` (from export) or by name. Duplicate names require the `id` column.
- Any row that does not match an existing CNAME blocks the run. Nothing is created or deleted.
- Proxied records must use TTL Auto. If you turn on `proxied` and leave TTL alone, TTL is set to Auto and the plan says so.
  An explicit conflicting TTL is rejected.
- Updates go through Cloudflare's batch endpoint in groups of 100 (each batch is all-or-nothing). `--no-batch` falls back to one request per record.
- Comments cannot be cleared by leaving the cell blank (blank means unchanged).
- Behind a TLS-inspecting proxy: set `HTTPS_PROXY`, and pass `--ca-bundle corp-ca.pem` (or set `SSL_CERT_FILE`).
