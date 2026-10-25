#!/usr/bin/env python3
"""Query Magpie quotas with compact, machine-readable output."""

import argparse
import json
import math
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone


STALE_SECONDS = 300


class QuotaError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise QuotaError(message)


def emit(value, stream=None):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), file=stream)


def text_value(value):
    value = str(value).replace("\\", "\\\\")
    value = value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return shlex.quote(value)


def emit_error(message, json_output):
    if json_output:
        emit({"error": message}, sys.stderr)
    else:
        print("error=" + text_value(message), file=sys.stderr)


def positive_timeout(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return seconds


def fetch_quotas(base_url, timeout):
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise QuotaError("url must be a gateway origin, e.g. http://127.0.0.1:3425")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/magpie/quotas",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + os.environ.get("MAGPIE_TOKEN", "magpie"),
        },
    )
    # Reach the gateway directly, even when WSL has HTTP proxy variables.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise QuotaError(f"gateway HTTP {exc.code}") from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise QuotaError("gateway unreachable or timed out; check MAGPIE_URL") from None
    except (ValueError, UnicodeError):
        raise QuotaError("gateway returned invalid JSON") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise QuotaError("invalid quota response")
    rows = payload["data"]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("provider"), str):
            raise QuotaError("invalid quota response")
        windows = row.get("windows", [])
        if not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
            raise QuotaError("invalid quota windows")
    return rows


def select_quotas(rows, provider):
    if provider is None:
        return rows
    needle = provider.casefold()
    exact = [row for row in rows if row["provider"].casefold() == needle]
    if exact:
        return exact
    matches = [
        row for row in rows
        if row["provider"].casefold().endswith("/" + needle)
    ]
    providers = sorted({row["provider"] for row in matches})
    if len(providers) > 1:
        raise QuotaError("ambiguous provider: " + ", ".join(providers))
    if not matches:
        available = ", ".join(sorted({row["provider"] for row in rows})) or "none"
        raise QuotaError("provider not found; available: " + available)
    return matches


def compact(row):
    result = {"provider": row["provider"]}
    for key in ("alias", "balance"):
        if row.get(key):
            result[key] = row[key]
    windows = []
    for window in row.get("windows", []):
        item = {"name": window.get("name", "")}
        if window.get("unlimited"):
            item["unlimited"] = True
        elif "remaining" in window:
            item["remaining_pct"] = window["remaining"]
        for key in ("resetsAt", "display", "amount", "limit", "unit", "pool"):
            if key in window:
                item[key] = window[key]
        windows.append(item)
    if windows:
        result["windows"] = windows
    # Preserve freshness and failure signals so cached data is not mistaken for live data.
    for key in ("readAt", "asOf", "readDeferred", "until", "error"):
        if row.get(key):
            result[key] = row[key]
    has_quota = row.get("balance") or any(
        w.get("unlimited") or w.get("remaining_pct") is not None for w in windows
    )
    if not has_quota and not row.get("error"):
        result["error"] = "quota unavailable"
    return result


def window_labels(windows):
    labels = []
    units = {"hours": "h", "hour": "h", "days": "d", "day": "d",
             "minutes": "m", "minute": "m", "weeks": "w", "week": "w"}
    for window in windows:
        name = window["name"].casefold()
        name = re.sub(
            r"\b(\d+)\s+(hours?|days?|minutes?|weeks?)\b",
            lambda match: match[1] + units[match[2]],
            name,
        )
        labels.append(re.sub(r"[^\w]+", "_", name).strip("_") or "quota")
    counts, seen = Counter(labels), Counter()
    result = []
    for label in labels:
        seen[label] += 1
        # Reserve numbered names when the normalized label would collide.
        candidate = f"{label}_{seen[label]}" if counts[label] > 1 else label
        while candidate in result or (candidate in counts and candidate != label):
            candidate += "_"
        result.append(candidate)
    return result


def reading_age(row, now):
    timestamp = row.get("readAt") or row.get("asOf")
    if not timestamp:
        return None
    try:
        at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if at.tzinfo is None:
            return None
        return max(0, int((now - at).total_seconds()))
    except (ValueError, TypeError):
        return None


def render_text(rows, all_providers, detail=False, now=None):
    now = now or datetime.now(timezone.utc)
    suffixes = Counter(provider.rsplit("/", 1)[-1] for provider in all_providers)
    accounts = Counter(row["provider"] for row in rows)
    aliases = Counter((row["provider"], row.get("alias")) for row in rows)
    seen = Counter()
    lines = []
    for row in rows:
        provider = row["provider"]
        suffix = provider.rsplit("/", 1)[-1]
        fields = [text_value(suffix if suffixes[suffix] == 1 else provider)]
        seen[provider] += 1
        alias = row.get("alias")
        if accounts[provider] > 1:
            account = alias if alias and aliases[provider, alias] == 1 else seen[provider]
            fields.append("account=" + text_value(account))
        elif alias:
            fields.append("account=" + text_value(alias))
        if row.get("balance"):
            fields.append("balance=" + text_value(row["balance"]))
        windows = row.get("windows", [])
        labels = window_labels(windows)
        for label, window in zip(labels, windows):
            remaining = window.get("remaining_pct")
            if window.get("unlimited"):
                value = "unlimited"
            elif remaining is None:
                value = "unknown"
            else:
                value = f"{remaining:g}%" if isinstance(remaining, (int, float)) else f"{remaining}%"
            fields.append(label + "=" + text_value(value))
        age = reading_age(row, now)
        stale = (
            row.get("asOf") or row.get("readDeferred")
            or (age is not None and age > STALE_SECONDS)
            or (row.get("error") and (windows or row.get("balance")))
        )
        if stale:
            fields.extend(["stale=true", f"age={age}s" if age is not None else "age=unknown"])
        if row.get("readDeferred"):
            fields.append("deferred=true")
        if row.get("error"):
            fields.append("error=" + text_value(row["error"]))
        if detail:
            fields.append("provider=" + text_value(provider))
            for key, label in (("readAt", "read"), ("asOf", "asof"), ("until", "until")):
                if row.get(key):
                    fields.append(label + "=" + text_value(row[key]))
            for label, window in zip(labels, windows):
                for key, short in (("resetsAt", "reset"), ("amount", "used"),
                                   ("limit", "limit"), ("unit", "unit"),
                                   ("display", "display"), ("pool", "pool")):
                    if key in window:
                        fields.append(f"{label}_{short}=" + text_value(window[key]))
        lines.append(" ".join(fields))
    return "\n".join(lines) or "empty"


def main(argv=None):
    raw_args = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in raw_args
    parser = Parser(description="Magpie quotas as compact text, one account per line.")
    parser.add_argument("provider", nargs="?", help="provider ID or unique suffix; omit for all")
    parser.add_argument("--list", action="store_true", help="list provider IDs only")
    parser.add_argument("--detail", action="store_true", help="include reset times and reading details")
    parser.add_argument("--json", action="store_true", help="output compact JSON instead of text")
    parser.add_argument("--url", default=os.environ.get("MAGPIE_URL", "http://127.0.0.1:3425"))
    parser.add_argument("--timeout", type=positive_timeout, default=20, metavar="SECONDS")
    try:
        args = parser.parse_args(raw_args)
    except QuotaError as exc:
        emit_error(str(exc), json_output)
        return 2
    try:
        all_rows = [
            row for row in fetch_quotas(args.url, args.timeout)
            if row["provider"].rsplit("/", 1)[-1] != "~keys"
        ]
        all_providers = sorted({row["provider"] for row in all_rows})
        rows = select_quotas(all_rows, args.provider)
        if args.list:
            providers = sorted({row["provider"] for row in rows})
            if args.json:
                emit(providers)
            else:
                print("\n".join(text_value(provider) for provider in providers) or "empty")
            return 0
        result = [compact(row) for row in rows]
        if args.json:
            emit(result)
        else:
            print(render_text(result, all_providers, args.detail))
        return int(any(row.get("error") for row in result))
    except QuotaError as exc:
        emit_error(str(exc), args.json)
        return 1
    except ValueError:
        emit_error("invalid gateway URL or request", args.json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
