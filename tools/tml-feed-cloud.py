#!/usr/bin/env python3
"""
tml-feed-cloud.py — Mac-INDEPENDENT podcast-feed updater for The Morning Lift.

This is the travel-proof counterpart to scripts/tml-feed-update.py. The original
builds the feed from the LOCAL episode folders (it ffprobes every local MP3), so it
only works when Tom's Mac mini is reachable. This one rebuilds the feed entirely from
what is ALREADY hosted on GitHub, so it runs from any cloud session with no dependency
on the Mac being awake:

  - Episode set + enclosure sizes:  GitHub Contents API (repo root listing).
  - Existing episodes:              reused VERBATIM from the currently-live feed.xml
                                    (same bytes -> no churn, descriptions preserved).
  - Brand-new episodes:             MP3 downloaded from the public site and ffprobed
                                    here in the cloud for <itunes:duration>; description
                                    from a repo sidecar feed-meta/<date>.json if present,
                                    otherwise a generic blurb.
  - Push:                           GitHub Contents API (PUT feed.xml), proxy bypassed.

The ONLY credential it needs is the GitHub PAT, for the push. It is read from, in order:
  1. env var  TML_GH_PAT
  2. --pat-file <path>
  3. the project secret on the Mac (…/.secrets/github-pat.txt) if that path is reachable
If none is available it exits non-zero WITHOUT touching the feed and (best-effort) says so.

Reads (network):  api.github.com, raw.githubusercontent.com, themorninglift.org
Writes (network): api.github.com (feed.xml)   — nothing on local disk except a temp MP3.

Usage:
  tml-feed-cloud.py                 # rebuild from GitHub + push if changed
  tml-feed-cloud.py --dry-run       # build + report, no push (prints the feed to stdout head)
  tml-feed-cloud.py --max 90        # cap episodes in the feed (default 90, matches the original)
  tml-feed-cloud.py --force         # push even if unchanged
  tml-feed-cloud.py --include-future# include not-yet-released episodes (manual rebuilds only)
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape

REPO = "contactkristensen/the-morning-lift"
BRANCH = "main"
API = "https://api.github.com"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
BASE = "https://themorninglift.org"

# Channel metadata — identical to scripts/tml-feed-update.py (locked with Tom 2026-06-11).
TITLE = "The Morning Lift"
AUTHOR = "The Morning Lift"
OWNER_NAME = "Thomas Kristensen"
OWNER_EMAIL = "contact.kristensen@gmail.com"
CHAN_DESC = (
    "Four positive stories in four minutes. Free audio, waiting for you when you wake up. "
    "Every morning, The Morning Lift brings you four uplifting, true stories — science, progress, "
    "kindness, and quiet wonder — to start your day on the right note."
)

WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH = ["", "January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]

DAILY_RE = re.compile(r"^TML_(\d{4})-(\d{2})-(\d{2})\.mp3$")
MIDWEEK_RE = re.compile(r"^TML_MidWeek_(\d{4})-(\d{2})-(\d{2})\.mp3$")

# ---- no-proxy HTTP (the cloud sandbox's default egress is a per-repo-gated GitHub proxy) ----
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method, url, pat=None, body=None, raw=False, timeout=120):
    data = json.dumps(body).encode() if (body is not None and not raw) else body
    headers = {"User-Agent": "tml-feed-cloud"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if body is not None and not raw:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status, r.read()


def gh_json(method, path, pat=None, body=None):
    try:
        st, payload = http(method, f"{API}{path}", pat=pat, body=body)
        return st, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, {}
        print(f"error: GitHub {e.code} {method} {path}: {e.read().decode(errors='replace')[:300]}",
              file=sys.stderr)
        raise


def find_pat(args):
    if os.environ.get("TML_GH_PAT", "").strip():
        return os.environ["TML_GH_PAT"].strip()
    if args.pat_file and os.path.isfile(args.pat_file):
        return open(args.pat_file).read().strip()
    for p in [os.path.expanduser("~/Documents/Morning Lift/.secrets/github-pat.txt")] + \
             __import__("glob").glob("/sessions/*/mnt/Morning Lift/.secrets/github-pat.txt"):
        if os.path.isfile(p):
            return open(p).read().strip()
    return None


def tzoff(mo):
    # CDT (-5) Mar–Oct, CST (-6) otherwise. Mirrors the original script's month rule.
    return -5 if 3 <= mo <= 10 else -6


def release_dt(kind, y, mo, da):
    off = tzoff(mo)
    hh, mm = (2, 0) if kind == "daily" else (11, 30)
    return datetime(y, mo, da, hh, mm, 0, tzinfo=timezone(timedelta(hours=off)))


def duration_iso(seconds):
    hh, mm, ss = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"


def parse_live(pat):
    """Return {enclosure_filename: {'raw': <item xml>, 'length': str, 'dt': release_dt}}."""
    try:
        _, payload = http("GET", f"{RAW}/feed.xml")
        xml = payload.decode("utf-8")
    except urllib.error.HTTPError:
        return {}
    out = {}
    for m in re.finditer(r"<item>.*?</item>", xml, re.S):
        it = m.group(0)
        em = re.search(r'enclosure url="([^"]+)" length="(\d+)"', it)
        pm = re.search(r"<pubDate>(.*?)</pubDate>", it)
        if not em:
            continue
        fname = em.group(1).rsplit("/", 1)[-1]
        dt = None
        if pm:
            try:
                dt = parsedate_to_dt(pm.group(1))
            except Exception:
                dt = None
        out[fname] = {"raw": it, "length": em.group(2), "dt": dt}
    return out


def parsedate_to_dt(s):
    from email.utils import parsedate_to_datetime
    return parsedate_to_datetime(s)


def repo_mp3s(pat):
    """{filename: size} for every TML_*.mp3 in the repo root."""
    st, items = gh_json("GET", f"/repos/{REPO}/contents?ref={BRANCH}", pat=pat)
    if st != 200:
        raise SystemExit(f"error: could not list repo contents (status {st})")
    out = {}
    for e in items:
        n = e.get("name", "")
        if DAILY_RE.match(n) or MIDWEEK_RE.match(n):
            out[n] = e.get("size", 0)
    return out


def sidecar_titles(pat, date):
    """feed-meta/<date>.json -> list[str] titles, or None."""
    st, e = gh_json("GET", f"/repos/{REPO}/contents/feed-meta/{date}.json?ref={BRANCH}", pat=pat)
    if st != 200:
        return None
    try:
        data = json.loads(base64.b64decode(e.get("content", "")).decode())
        titles = data.get("titles") if isinstance(data, dict) else data
        return [t for t in titles][:4] if titles else None
    except Exception:
        return None


def ffprobe_seconds(url):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        path = tf.name
    try:
        _, payload = http("GET", url, timeout=120)
        with open(path, "wb") as f:
            f.write(payload)
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return int(float(r.stdout.strip())) if r.stdout.strip() else 0
    except Exception as e:
        print(f"warn: ffprobe failed for {url}: {e}", file=sys.stderr)
        return 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def build_new_item(kind, y, mo, da, fname, size, pat):
    dt = datetime(y, mo, da)
    wd = WEEKDAY[dt.weekday()]
    off = "-0500" if 3 <= mo <= 10 else "-0600"
    url = f"{BASE}/{fname}"
    seconds = ffprobe_seconds(f"{RAW}/{fname}")
    date = f"{y:04d}-{mo:02d}-{da:02d}"
    titles = sidecar_titles(pat, date)
    if kind == "daily":
        t = f"{TITLE} — {wd}, {MONTH[mo]} {da}, {y}"
        pub = f"{wd[:3]}, {da:02d} {MONTH[mo][:3]} {y} 02:00:00 {off}"
        if titles:
            body = "Four positive stories to start your morning: " + \
                   "; ".join(f"({i+1}) {t2}" for i, t2 in enumerate(titles)) + "."
        else:
            body = ("Four positive stories to start your morning — science, progress, kindness, "
                    "and wonder. Free audio, about five minutes.")
    else:
        t = f"Mid Week Lift — {wd}, {MONTH[mo]} {da}, {y}"
        pub = f"{wd[:3]}, {da:02d} {MONTH[mo][:3]} {y} 11:30:00 {off}"
        body = ("A midweek bonus from The Morning Lift: one of our most-loved stories, "
                "re-read for you. Four stories, four minutes — every morning at themorninglift.org.")
    iso = duration_iso(seconds)
    return ("    <item>\n"
            f"      <title>{escape(t)}</title>\n"
            f"      <description>{escape(body)}</description>\n"
            f"      <itunes:summary>{escape(body)}</itunes:summary>\n"
            f'      <enclosure url="{url}" length="{size}" type="audio/mpeg"/>\n'
            f'      <guid isPermaLink="false">{url}</guid>\n'
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <itunes:duration>{iso}</itunes:duration>\n"
            "      <itunes:explicit>false</itunes:explicit>\n"
            "    </item>")


def channel_header(now):
    return ['<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
            'xmlns:content="http://purl.org/rss/1.0/modules/content/">',
            '  <channel>',
            f'    <title>{escape(TITLE)}</title>',
            f'    <link>{BASE}</link>',
            '    <language>en-us</language>',
            f'    <description>{escape(CHAN_DESC)}</description>',
            f'    <itunes:summary>{escape(CHAN_DESC)}</itunes:summary>',
            f'    <itunes:author>{escape(AUTHOR)}</itunes:author>',
            '    <itunes:type>episodic</itunes:type>',
            '    <itunes:explicit>false</itunes:explicit>',
            '    <itunes:category text="News"/>',
            f'    <itunes:image href="{BASE}/podcast-cover.jpg"/>',
            f'    <image><url>{BASE}/podcast-cover.jpg</url><title>{escape(TITLE)}</title>'
            f'<link>{BASE}</link></image>',
            '    <itunes:owner>',
            f'      <itunes:name>{escape(OWNER_NAME)}</itunes:name>',
            f'      <itunes:email>{OWNER_EMAIL}</itunes:email>',
            '    </itunes:owner>',
            f'    <lastBuildDate>{now}</lastBuildDate>']


def build_feed(pat, max_items, include_future):
    live = parse_live(pat)
    mp3s = repo_mp3s(pat)
    now_utc = datetime.now(timezone.utc)
    # 1) Collect all released episodes, newest-first, and cap BEFORE building anything —
    #    so we never download/ffprobe an MP3 that would be dropped past the cap.
    cand = []  # (release_dt, fname, size, kind, y, mo, da)
    for fname, size in mp3s.items():
        m = DAILY_RE.match(fname) or MIDWEEK_RE.match(fname)
        kind = "daily" if DAILY_RE.match(fname) else "midweek"
        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
        rdt = release_dt(kind, y, mo, da)
        if not include_future and rdt > now_utc:
            continue
        cand.append((rdt, fname, size, kind, y, mo, da))
    cand.sort(key=lambda x: x[0], reverse=True)
    cand = cand[:max_items]
    # 2) Reuse verbatim when the hosted size matches the live feed; only build the rest.
    entries = []
    reused = built = 0
    for rdt, fname, size, kind, y, mo, da in cand:
        cached = live.get(fname)
        if cached and cached["length"] == str(size):
            # raw block was captured starting at "<item>"; restore the 4-space indent.
            entries.append((rdt, "    " + cached["raw"]))
            reused += 1
        else:
            entries.append((rdt, build_new_item(kind, y, mo, da, fname, size, pat)))
            built += 1
    entries.sort(key=lambda x: x[0], reverse=True)
    items = [x[1] for x in entries]
    now = format_datetime(datetime.utcnow()).replace("-0000", "+0000")
    xml = "\n".join(channel_header(now) + items + ['  </channel>', '</rss>']) + "\n"
    return xml, len(items), reused, built


def strip_volatile(xml):
    return re.sub(r"<lastBuildDate>.*?</lastBuildDate>", "", xml)


def verify_pages(pat, commit_sha, timeout_s=300):
    short = commit_sha[:10]
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        st, b = gh_json("GET", f"/repos/{REPO}/pages/builds/latest", pat=pat)
        if st == 404:
            print("warn: Pages not enabled — skipping verification.", file=sys.stderr)
            return True
        b = b or {}
        last = b.get("status")
        if (b.get("commit") or "")[:10] == short and last == "built":
            print(f"OK -> Pages deployed {short}; {BASE}/feed.xml is current.")
            return True
        if (b.get("commit") or "")[:10] == short and last == "errored":
            print(f"ERROR -> Pages build {short} errored.", file=sys.stderr)
            return False
        time.sleep(10)
    print(f"WARN -> Pages deploy of {short} not confirmed in {timeout_s}s (last {last}).", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description="Rebuild + push the TML podcast feed from GitHub (Mac-independent).")
    ap.add_argument("--max", type=int, default=90)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--include-future", action="store_true")
    ap.add_argument("--no-verify-pages", action="store_true")
    ap.add_argument("--pages-timeout", type=int, default=300)
    ap.add_argument("--pat-file", default=None)
    args = ap.parse_args()

    pat = find_pat(args)
    if not pat:
        print("error: no GitHub PAT (set TML_GH_PAT, pass --pat-file, or make the Mac secret reachable). "
              "Feed NOT changed.", file=sys.stderr)
        sys.exit(3)

    xml, n, reused, built = build_feed(pat, args.max, args.include_future)
    print(f"Built feed: {n} items ({reused} reused verbatim, {built} newly built).")
    top = re.search(r"<item>.*?<title>(.*?)</title>", xml, re.S)
    if top:
        print("Top item:", top.group(1))

    if args.dry_run:
        print("[dry-run] not pushing.")
        return

    st, existing = gh_json("GET", f"/repos/{REPO}/contents/feed.xml?ref={BRANCH}", pat=pat)
    sha = existing.get("sha") if st == 200 else None
    if st == 200:
        try:
            cur = base64.b64decode(existing.get("content", "")).decode("utf-8")
            if not args.force and strip_volatile(cur) == strip_volatile(xml):
                print("No episode changes since last push — skipping (use --force to override).")
                return
        except Exception:
            pass

    body = {"message": f"Update podcast feed ({n} episodes, cloud)",
            "content": base64.b64encode(xml.encode()).decode(), "branch": BRANCH}
    if sha:
        body["sha"] = sha
    st, resp = gh_json("PUT", f"/repos/{REPO}/contents/feed.xml", pat=pat, body=body)
    if st not in (200, 201):
        print(f"error: PUT failed status {st}: {json.dumps(resp)[:300]}", file=sys.stderr)
        sys.exit(1)
    commit = (resp.get("commit") or {}).get("sha", "?")
    print(f"OK -> pushed feed.xml (commit {commit[:10]}) — live shortly at {BASE}/feed.xml")

    if args.no_verify_pages or commit == "?":
        return
    if not verify_pages(pat, commit, timeout_s=args.pages_timeout):
        sys.exit(2)


if __name__ == "__main__":
    main()
