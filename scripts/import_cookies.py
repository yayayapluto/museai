#!/usr/bin/env python3
"""Import muse.ai browser cookies into a running museai instance.

    # Cookie-Editor JSON, cookies.txt or a raw "a=b; c=d" header
    python scripts/import_cookies.py cookies.json --label laptop

    # update an existing account instead of creating one
    python scripts/import_cookies.py cookies.txt --account acc_1234

The parser is the same one the admin API uses, so whatever Chrome/Firefox/Edge or the
Cookie-Editor extension hands you works without preprocessing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from museai.core.cookies import CookieParseError, missing_required, parse_any  # noqa: E402

HINTS = (
    "Log in at https://muse.ai in a browser, then export the cookies for muse.ai "
    "(DevTools > Application > Cookies, or the Cookie-Editor extension) and pass the file here. "
    "Required cookies: hatch_sess, hatch_gw, hatch_native_auth_device."
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", nargs="?", help="cookie export file; omit to read stdin")
    ap.add_argument("--label", default="", help="account label")
    ap.add_argument("--account", default=None, help="update this account id instead of creating")
    ap.add_argument("--url", default=os.environ.get("MUSE2API_URL", "http://127.0.0.1:18610"))
    ap.add_argument("--key", default=os.environ.get("MUSE2API_ADMIN_KEY"), help="admin bearer key")
    args = ap.parse_args()

    key = args.key or _key_from_data_dir()
    if not key:
        print("missing admin key: pass --key or set MUSE2API_ADMIN_KEY", file=sys.stderr)
        return 2

    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    try:
        cookies = parse_any(text)
    except CookieParseError as exc:
        print(f"could not parse cookies: {exc}\n\n{HINTS}", file=sys.stderr)
        return 2
    missing = missing_required(cookies)
    if missing:
        print(f"warning: missing cookies {missing}\n\n{HINTS}", file=sys.stderr)

    body: dict = {"cookies": cookies}
    if args.label:
        body["label"] = args.label
    headers = {"Authorization": f"Bearer {key}"}
    if args.account:
        resp = httpx.patch(f"{args.url}/admin/accounts/{args.account}", json=body,
                           headers=headers, timeout=30)
    else:
        resp = httpx.post(f"{args.url}/admin/accounts", json=body, headers=headers, timeout=30)
    if resp.status_code >= 400:
        print(f"import failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        return 1
    data = resp.json()
    print(json.dumps({"account": data["account"]["id"], "label": data["account"]["label"],
                      "missing_cookies": data.get("missing_cookies", missing)}, indent=2))
    return 0


def _key_from_data_dir() -> str | None:
    path = Path(os.environ.get("MUSE2API_DATA_DIR", "data")) / "api_key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return os.environ.get("MUSE2API_API_KEY") or None


if __name__ == "__main__":
    raise SystemExit(main())
