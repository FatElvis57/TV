#!/usr/bin/env python3
"""Refresh matching stream URLs in the repository's root Channels M3U.

- Preserves the custom channel selection, order, EXTINF metadata, and comments.
- Updates URLs only when tvg-id matches an upstream iptv-org entry.
- Leaves unmatched entries unchanged and reports them; it does not guess matches.
"""
from pathlib import Path
from urllib.request import Request, urlopen
import re
import sys

UPSTREAM = "https://iptv-org.github.io/iptv/index.m3u"
TARGET = Path("Channels")


def fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "GitHub-Actions-IPTV-Playlist-Updater/1.0"})
    with urlopen(req, timeout=90) as response:
        if response.status != 200:
            raise RuntimeError(f"Upstream returned HTTP {response.status}")
        return response.read().decode("utf-8-sig", errors="replace")


def parse_entries(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    entries = []
    current_id = None
    for line in lines:
        if line.startswith("#EXTINF:"):
            match = re.search(r'\btvg-id="([^"]*)"', line)
            current_id = match.group(1).strip() if match else None
        elif line.strip() and not line.lstrip().startswith("#"):
            if current_id:
                entries.append((current_id, line.strip()))
            current_id = None
    return entries


def main():
    if not TARGET.exists():
        sys.exit("ERROR: root file 'Channels' was not found.")

    local_text = TARGET.read_text(encoding="utf-8-sig")
    upstream_text = fetch(UPSTREAM)
    upstream_entries = parse_entries(upstream_text)

    # Keep only unambiguous IDs. If upstream has multiple URLs for an ID,
    # don't guess which one should replace the user's chosen stream.
    urls_by_id = {}
    ambiguous = set()
    for channel_id, url in upstream_entries:
        if channel_id in urls_by_id and urls_by_id[channel_id] != url:
            ambiguous.add(channel_id)
        else:
            urls_by_id[channel_id] = url
    for channel_id in ambiguous:
        urls_by_id.pop(channel_id, None)

    lines = local_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    updated = 0
    unmatched = 0
    current_id = None
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            match = re.search(r'\btvg-id="([^"]*)"', line)
            current_id = match.group(1).strip() if match else None
        elif line.strip() and not line.lstrip().startswith("#"):
            if current_id:
                new_url = urls_by_id.get(current_id)
                if new_url:
                    if line.strip() != new_url:
                        lines[i] = new_url
                        updated += 1
                else:
                    unmatched += 1
            current_id = None

    output = "\n".join(lines).rstrip() + "\n"
    if output != local_text:
        TARGET.write_text(output, encoding="utf-8")
    print(f"Upstream entries with IDs: {len(upstream_entries)}")
    print(f"Local stream URLs updated: {updated}")
    print(f"Local entries not matched to a unique upstream tvg-id: {unmatched}")
    print("Unmatched entries were left untouched; no channel names were guessed.")
    if not upstream_entries:
        sys.exit("ERROR: Upstream playlist parsed as empty; refusing to modify file.")


if __name__ == "__main__":
    main()
