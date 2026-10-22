
#!/usr/bin/env python3
"""Build a combined XMLTV guide without replacing a valid guide on failure."""

import gzip
import json
import re
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "epg-config.json"
STATUS_PATH = ROOT / "epg-status.json"
USER_AGENT = "FatElvis57-TV-EPG/1.0"
TIMEOUT = 60


def now_utc():
    return datetime.now(timezone.utc)


def parse_xmltv_time(value):
    if not value:
        return None
    value = value.strip()
    match = re.match(
        r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?"
        r"\s*([+-]\d{4}|Z)?$",
        value,
    )
    if not match:
        return None

    year, month, day, hour, minute, second, zone = match.groups()
    second = second or "00"
    try:
        dt = datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second)
        )
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            offset = timedelta(
                hours=int(zone[1:3]), minutes=int(zone[3:5])
            ) * sign
            dt = dt.replace(tzinfo=timezone(offset))
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def normalized(value):
    value = (value or "").casefold()
    value = re.sub(r"\b(pluto tv|pluto)\b", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read()

    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)

    if not data.strip():
        raise ValueError("Downloaded file is empty")
    return data


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    if not config.get("playlist_url"):
        raise ValueError("Config is missing playlist_url")
    if not config.get("sources"):
        raise ValueError("Config contains no EPG sources")
    return config


def parse_m3u(data):
    text = data.decode("utf-8-sig", errors="replace")
    channels = []
    pending = None

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            attrs = dict(
                re.findall(r'([\w-]+)="([^"]*)"', line)
            )
            display = line.split(",", 1)[1].strip() if "," in line else ""
            pending = {
                "tvg_id": attrs.get("tvg-id", "").strip(),
                "tvg_name": attrs.get("tvg-name", "").strip(),
                "name": display,
                "tvg_logo": attrs.get("tvg-logo", "").strip(),
                "url": "",
            }
        elif line and not line.startswith("#") and pending is not None:
            pending["url"] = line
            pending["guide_id"] = (
                pending["tvg_id"]
                or pending["tvg_name"]
                or pending["name"]
            )
            if pending["guide_id"]:
                channels.append(pending)
            pending = None

    if not channels:
        raise ValueError("No channels were parsed from the M3U playlist")
    return channels


def source_channel_names(element):
    names = []
    for child in element.findall("display-name"):
        if child.text and child.text.strip():
            names.append(child.text.strip())
    return names


def parse_source(data):
    root = ET.fromstring(data)
    if root.tag != "tv":
        raise ValueError("Source XML root is not <tv>")

    channels = {}
    for channel in root.findall("channel"):
        channel_id = (channel.get("id") or "").strip()
        if channel_id:
            channels[channel_id] = {
                "element": channel,
                "names": source_channel_names(channel),
            }

    programmes = []
    for programme in root.findall("programme"):
        channel_id = (programme.get("channel") or "").strip()
        start = parse_xmltv_time(programme.get("start"))
        stop = parse_xmltv_time(programme.get("stop"))
        if not channel_id or not start or not stop or stop <= start:
            continue
        programmes.append((channel_id, programme, start, stop))

    if not channels:
        raise ValueError("Source contains no XMLTV channels")
    return channels, programmes


def build_channel_matches(playlist, source_channels, id_map):
    """Match explicit mappings and IDs first, then unique exact names."""
    matches = {}
    source_ids = set(source_channels)

    for item in playlist:
        target_id = item["guide_id"]
        explicit = id_map.get(target_id)

        if explicit:
            if explicit in source_ids:
                matches[target_id] = explicit
            continue

        # Match an existing M3U tvg-id only when it exactly exists in the feed.
        if item["tvg_id"] and item["tvg_id"] in source_ids:
            matches[target_id] = item["tvg_id"]
            continue

        # Try a Pluto ID embedded in the stream URL if the feed uses that ID.
        url_match = re.search(r"(?:plu-)?([a-f0-9]{24})", item["url"], re.I)
        if url_match:
            candidate = url_match.group(1)
            for source_id in source_ids:
                if source_id.casefold() in {
                    candidate.casefold(),
                    ("plu-" + candidate).casefold(),
                    ("pluto-" + candidate).casefold(),
                }:
                    matches[target_id] = source_id
                    break
            if target_id in matches:
                continue

        wanted_names = {
            normalized(item.get("tvg_name")),
            normalized(item.get("name")),
        }
        wanted_names.discard("")
        candidates = []

        for source_id, source in source_channels.items():
            source_names = {normalized(n) for n in source["names"]}
            source_names.discard("")
            if wanted_names & source_names:
                candidates.append(source_id)

        # Never choose arbitrarily when multiple feed channels have the name.
        if len(candidates) == 1:
            matches[target_id] = candidates[0]

    return matches


def write_status(status):
    temporary = STATUS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATUS_PATH)


def main():
    status = {
        "generated_at_utc": now_utc().isoformat(),
        "result": "failed",
        "sources": [],
        "playlist_channels": 0,
        "matched_channels": 0,
        "programme_count": 0,
        "unmatched_channels": [],
        "warnings": [],
    }

    try:
        config = load_config()
        playlist = parse_m3u(fetch(config["playlist_url"]))
        status["playlist_channels"] = len(playlist)

        current = now_utc()
        start_limit = current - timedelta(
            days=int(config.get("programme_days_past", 1))
        )
        end_limit = current + timedelta(
            days=int(config.get("programme_days_future", 7))
        )

        # Store the highest-priority programme for each channel/time slot.
        combined = {}
        channel_metadata = {}
        matched_any = set()
        successful_sources = 0

        sources = sorted(
            [s for s in config["sources"] if s.get("enabled", True)],
            key=lambda s: int(s.get("priority", 999)),
        )

        for source in sources:
            source_name = source.get("name", "Unnamed source")
            source_url = source.get("url", "")
            report = {"name": source_name, "url": source_url, "result": "failed"}

            try:
                if not source_url:
                    raise ValueError("Source URL is missing")
                source_channels, programmes = parse_source(fetch(source_url))
                successful_sources += 1
                report["result"] = "downloaded"
                report["source_channels"] = len(source_channels)
                report["source_programmes"] = len(programmes)

                id_map = source.get("channel_id_map", {}) or {}
                matches = build_channel_matches(
                    playlist, source_channels, id_map
                )
                report["matched_channels"] = len(matches)
                matched_any.update(matches.keys())

                for item in playlist:
                    target_id = item["guide_id"]
                    source_id = matches.get(target_id)
                    if not source_id:
                        continue

                    if target_id not in channel_metadata:
                        source_element = source_channels[source_id]["element"]
                        out_channel = ET.Element(
                            "channel", {"id": target_id}
                        )
                        display = ET.SubElement(out_channel, "display-name")
                        display.text = item["name"] or item["tvg_name"] or target_id
                        logo = item.get("tvg_logo", "")
                        channel_metadata[target_id] = out_channel

                    for programme_id, programme, begins, ends in programmes:
                        if programme_id != source_id:
                            continue
                        if ends <= start_limit or begins >= end_limit:
                            continue

                        # Preserve source content, but assign the playlist guide ID.
                        out = ET.fromstring(ET.tostring(programme, encoding="utf-8"))
                        out.set("channel", target_id)
                        key = (target_id, programme.get("start", ""), programme.get("stop", ""))
                        # Earlier configured priority wins if slots overlap.
                        if key not in combined:
                            combined[key] = out

            except Exception as exc:
                report["error"] = str(exc)
                status["warnings"].append(
                    f"{source_name}: {exc}"
                )

            status["sources"].append(report)

        status["matched_channels"] = len(matched_any)
        status["unmatched_channels"] = [
            item["guide_id"] for item in playlist
            if item["guide_id"] not in matched_any
        ]

        if successful_sources == 0:
            raise ValueError("All enabled EPG sources failed")

        minimum = int(config.get("minimum_valid_programmes", 1))
        if len(combined) < minimum:
            raise ValueError(
                f"Only {len(combined)} programmes were matched; "
                f"minimum required is {minimum}"
            )

        tv = ET.Element("tv", {"generator-info-name": "FatElvis57-TV EPG"})
        for channel_id in sorted(channel_metadata):
            tv.append(channel_metadata[channel_id])
        for key in sorted(combined):
            tv.append(combined[key])

        ET.indent(tv, space="  ")
        xml_bytes = ET.tostring(
            tv, encoding="utf-8", xml_declaration=True
        )

        # Validate the complete XML before touching the existing guide.
        check_root = ET.fromstring(xml_bytes)
        if check_root.tag != "tv":
            raise ValueError("Generated XML failed validation")

        output_name = config.get("output_file", "Guide.xml")
        output_path = ROOT / output_name
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_path.parent,
            prefix=output_path.name + ".", suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(xml_bytes)
            temp_path = Path(temporary.name)

        # Re-parse the exact temporary file before replacing the guide.
        ET.parse(temp_path)
        temp_path.replace(output_path)

        status["result"] = "success"
        status["programme_count"] = len(combined)
        status["output_file"] = output_name
        status["output_bytes"] = len(xml_bytes)
        write_status(status)

        print(
            f"Success: {len(channel_metadata)} channels, "
            f"{len(combined)} programmes written to {output_name}"
        )
        if status["unmatched_channels"]:
            print(
                f"Warning: {len(status['unmatched_channels'])} playlist "
                "channels did not match an EPG source."
            )

    except Exception as exc:
        status["error"] = str(exc)
        try:
            write_status(status)
        except Exception:
            pass
        print(f"EPG BUILD FAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

