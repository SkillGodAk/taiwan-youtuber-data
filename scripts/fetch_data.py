#!/usr/bin/env python3
"""Update Taiwan YouTuber ranking data.

The app needs two different jobs:

1. Refresh known channel statistics cheaply every 10 minutes.
2. Grow the searchable Taiwan channel pool over time without burning quota.

External ranking sites are used only as candidate sources. Final subscriber
counts, ordering, and optional country checks always come from YouTube Data API.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any


API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
BASE_URL = "https://www.googleapis.com/youtube/v3"

TOP_CHANNEL_LIMIT = int(os.environ.get("TOP_CHANNEL_LIMIT", "100"))
RANKING_POOL_SIZE = int(os.environ.get("RANKING_POOL_SIZE", "500"))
LATEST_VIDEO_CHANNEL_LIMIT = int(os.environ.get("LATEST_VIDEO_CHANNEL_LIMIT", "20"))
LATEST_VIDEO_COUNT = int(os.environ.get("LATEST_VIDEO_COUNT", "3"))

DISCOVERY_MODE = os.environ.get("DISCOVERY_MODE", "").lower() in ("1", "true", "yes")
CANDIDATE_LIMIT = int(os.environ.get("CANDIDATE_LIMIT", "1000"))
CANDIDATE_RESOLVE_LIMIT = int(os.environ.get("CANDIDATE_RESOLVE_LIMIT", "25"))
ABOUT_PAGE_CHECK_LIMIT = int(os.environ.get("ABOUT_PAGE_CHECK_LIMIT", "120"))
VERIFY_OFFICIAL_TW = os.environ.get("VERIFY_OFFICIAL_TW", "true").lower() in (
    "1",
    "true",
    "yes",
)

YOUTUBERS_ME_URL = (
    "https://us.youtubers.me/taiwan/all/top-1000-youtube-channels-in-taiwan"
)
NOXINFLUENCER_URL = "https://www.noxinfluencer.com/ws/rank/youtube/kol"

CHANNELS_FILE = "data/channels.json"
CANDIDATES_FILE = "data/candidate_channels.json"
HISTORY_FILE = "data/history.json"
PREVIOUS_RANKS_FILE = "data/previous_ranks.json"

DISCOVERY_KEYWORDS = [
    "YouTuber",
    "vlog",
    "美食",
    "遊戲",
    "新聞",
    "旅遊",
    "開箱",
    "音樂",
    "生活",
    "短影音",
]

TAIWAN_TEXT_PATTERNS = [
    "台灣",
    "臺灣",
    "Taiwan",
    "Taiwanese",
    "Taipei",
    "New Taipei",
    "Kaohsiung",
    "Taichung",
    "Tainan",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


def http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = "utf-8"
        content_type = resp.headers.get("Content-Type", "")
        match = re.search(r"charset=([A-Za-z0-9_-]+)", content_type)
        if match:
            charset = match.group(1)
        return raw.decode(charset, errors="replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_int(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else 0


def normalize_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def title_similarity(left: str, right: str) -> float:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.86
    return SequenceMatcher(None, a, b).ratio()


def youtube_api_get(endpoint: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if not API_KEY:
        print("YOUTUBE_API_KEY is missing")
        return None

    query = dict(params)
    query["key"] = API_KEY
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        print(f"YouTube API HTTP {exc.code} ({endpoint}): {body}")
    except Exception as exc:
        print(f"YouTube API error ({endpoint}): {exc}")
    return None


def load_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def fetch_youtubers_me_candidates(limit: int = CANDIDATE_LIMIT) -> list[dict[str, Any]]:
    print(f"Fetching youtubers.me Taiwan top {limit} candidates")
    try:
        page = http_get_text(YOUTUBERS_ME_URL)
    except Exception as exc:
        print(f"youtubers.me fetch failed: {exc}")
        return []

    rows: list[dict[str, Any]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, flags=re.I | re.S):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        if len(cells) < 7:
            continue

        rank_text = strip_tags(cells[0])
        if not rank_text.isdigit():
            continue

        link_match = re.search(r'href=["\']([^"\']+)["\']', cells[1], flags=re.I)
        name = strip_tags(cells[1])
        if not name:
            continue

        rows.append(
            {
                "name": name,
                "source": "youtubers.me",
                "sourceRank": int(rank_text),
                "sourceSubscribersText": strip_tags(cells[2]),
                "sourceViewsText": strip_tags(cells[3]),
                "sourceVideosText": strip_tags(cells[4]),
                "category": strip_tags(cells[5]),
                "started": strip_tags(cells[6]),
                "href": link_match.group(1) if link_match else "",
            }
        )
        if len(rows) >= limit:
            break

    print(f"Loaded {len(rows)} youtubers.me candidates")
    return rows


def fetch_noxinfluencer_channels(limit: int = RANKING_POOL_SIZE) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "country": "TW",
            "rankType": "followers",
            "interval": "weekly",
            "pageNum": 1,
            "pageSize": min(limit, 100),
        }
    )
    try:
        text = http_get_text(f"{NOXINFLUENCER_URL}?{params}")
        data = json.loads(text)
    except Exception as exc:
        print(f"Noxinfluencer fetch failed: {exc}")
        return []

    if isinstance(data, dict):
        rows = data.get("retDataList") or data.get("data") or data.get("rows") or []
    else:
        rows = data if isinstance(data, list) else []

    channels = []
    for row in rows[:limit]:
        cid = row.get("channelId") or row.get("id") or row.get("channel_id")
        title = row.get("title") or row.get("name") or row.get("channelName") or ""
        if cid:
            channels.append(
                {
                    "channel_id": cid,
                    "title": title,
                    "source": "noxinfluencer",
                    "avg_views": parse_int(
                        row.get("avgViews") or row.get("avg_views") or row.get("avgView")
                    ),
                }
            )
    print(f"Loaded {len(channels)} Noxinfluencer candidates")
    return channels


def load_known_channels() -> list[dict[str, Any]]:
    data = load_json_file(CHANNELS_FILE, {"channels": []})
    channels = []
    for row in data.get("channels", []):
        cid = row.get("id") or row.get("channel_id")
        if cid:
            channels.append(
                {
                    "channel_id": cid,
                    "title": row.get("title", ""),
                    "source": "known",
                    "avg_views": row.get("avgViews", 0),
                }
            )
    return channels


def load_candidate_cache() -> dict[str, dict[str, Any]]:
    data = load_json_file(CANDIDATES_FILE, {"candidates": []})
    cache: dict[str, dict[str, Any]] = {}
    for row in data.get("candidates", []):
        name = row.get("name", "")
        if name:
            cache[normalize_name(name)] = row
    return cache


def save_candidate_cache(rows: list[dict[str, Any]]) -> None:
    rows = sorted(rows, key=lambda c: (c.get("sourceRank") or 999999, c.get("name", "")))
    write_json_file(CANDIDATES_FILE, {"updatedAt": int(time.time()), "candidates": rows})


def search_channel_candidates(query: str) -> list[str]:
    data = youtube_api_get(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "channel",
            "regionCode": "TW",
            "maxResults": 5,
            "order": "relevance",
        },
    )
    if not data:
        return []
    ids = []
    for item in data.get("items", []):
        cid = item.get("id", {}).get("channelId", "")
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def batch_channels_list(channel_ids: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    unique_ids = list(dict.fromkeys([cid for cid in channel_ids if cid]))
    for index in range(0, len(unique_ids), 50):
        batch = unique_ids[index : index + 50]
        data = youtube_api_get(
            "channels",
            {
                "part": "snippet,statistics,brandingSettings,contentDetails",
                "id": ",".join(batch),
            },
        )
        if data and "items" in data:
            results.extend(data["items"])
        time.sleep(0.12)
    return results


def official_country(channel: dict[str, Any]) -> str:
    branding_country = (
        channel.get("brandingSettings", {}).get("channel", {}).get("country", "")
    )
    snippet_country = channel.get("snippet", {}).get("country", "")
    return (branding_country or snippet_country or "").upper()


def text_has_taiwan_signal(value: str) -> bool:
    normalized = value.casefold()
    return any(pattern.casefold() in normalized for pattern in TAIWAN_TEXT_PATTERNS)


def fetch_about_page_taiwan_reason(channel: dict[str, Any]) -> str:
    """Check YouTube's public About page for Taiwan signals.

    Data API country is the first choice, but YouTube's web About panel can show
    a country even when Data API metadata is blank. This quota-free check is
    intentionally limited by ABOUT_PAGE_CHECK_LIMIT.
    """
    cid = channel.get("id", "")
    snippet = channel.get("snippet", {})
    custom_url = snippet.get("customUrl", "")
    urls = []
    if custom_url:
        handle = custom_url if custom_url.startswith("@") else f"@{custom_url}"
        urls.append(f"https://www.youtube.com/{handle}/about")
    if cid:
        urls.append(f"https://www.youtube.com/channel/{cid}/about")

    for url in urls:
        try:
            page = http_get_text(url, timeout=20)
        except Exception:
            continue

        compact = page.replace("\\u0026", "&")
        country_match = re.search(
            r'"country"\s*:\s*\{[^}]*"simpleText"\s*:\s*"([^"]+)"',
            compact,
            flags=re.I,
        )
        if country_match and text_has_taiwan_signal(country_match.group(1)):
            return "youtube_about_country"

        meta_match = re.search(
            r'<meta\s+name="description"\s+content="([^"]+)"',
            compact,
            flags=re.I,
        )
        if meta_match and text_has_taiwan_signal(html.unescape(meta_match.group(1))):
            return "youtube_about_description"

        title_match = re.search(r"<title>(.*?)</title>", compact, flags=re.I | re.S)
        if title_match and text_has_taiwan_signal(strip_tags(title_match.group(1))):
            return "youtube_about_title"

    return ""


def taiwan_match_reason(
    channel: dict[str, Any],
    seed: dict[str, Any] | None = None,
    allow_about_page: bool = False,
) -> str:
    country = official_country(channel)
    if country == "TW":
        return "official_country_tw"

    snippet = channel.get("snippet", {})
    title = snippet.get("title", "")
    description = snippet.get("description", "")
    custom_url = snippet.get("customUrl", "")
    seed_title = (seed or {}).get("title", "")

    if text_has_taiwan_signal(f"{title} {seed_title}"):
        return "title_taiwan_signal"
    if text_has_taiwan_signal(description):
        return "description_taiwan_signal"
    if text_has_taiwan_signal(custom_url):
        return "handle_taiwan_signal"

    if allow_about_page:
        return fetch_about_page_taiwan_reason(channel)

    return ""


def resolve_youtubers_candidates(
    candidates: list[dict[str, Any]], cache: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve a quota-limited number of candidate names to official channel IDs."""
    merged: dict[str, dict[str, Any]] = dict(cache)
    unresolved = []

    for candidate in candidates:
        key = normalize_name(candidate["name"])
        current = merged.get(key, {})
        current.update({k: v for k, v in candidate.items() if v not in ("", None)})
        current.setdefault("name", candidate["name"])
        merged[key] = current
        if not current.get("channel_id") and current.get("status") != "not_found":
            unresolved.append(current)

    if not DISCOVERY_MODE:
        print("Discovery is off; keeping cached youtubers.me resolutions only")
        return list(merged.values())

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    to_resolve = unresolved[:CANDIDATE_RESOLVE_LIMIT]
    print(f"Resolving {len(to_resolve)} youtubers.me candidates via YouTube search")

    for candidate in to_resolve:
        ids = search_channel_candidates(candidate["name"])
        details = batch_channels_list(ids)

        best: dict[str, Any] | None = None
        best_score = 0.0
        for detail in details:
            title = detail.get("snippet", {}).get("title", "")
            score = title_similarity(candidate["name"], title)
            if official_country(detail) == "TW":
                score += 0.15
            if score > best_score:
                best = detail
                best_score = score

        candidate["lastResolved"] = today
        if best and best_score >= 0.62:
            country = official_country(best)
            candidate["channel_id"] = best["id"]
            candidate["officialTitle"] = best.get("snippet", {}).get("title", "")
            candidate["officialCountry"] = country
            candidate["matchScore"] = round(best_score, 3)
            candidate["status"] = "verified" if country == "TW" else "resolved_non_tw"
        else:
            candidate["status"] = "not_found"

        time.sleep(0.2)

    return list(merged.values())


def discover_channels_by_youtube_search(existing_ids: set[str]) -> list[dict[str, Any]]:
    discovered = []
    for keyword in DISCOVERY_KEYWORDS:
        data = youtube_api_get(
            "search",
            {
                "part": "snippet",
                "q": keyword,
                "type": "channel",
                "regionCode": "TW",
                "order": "viewCount",
                "maxResults": 50,
            },
        )
        if not data:
            continue
        for item in data.get("items", []):
            cid = item.get("id", {}).get("channelId", "")
            if cid and cid not in existing_ids:
                existing_ids.add(cid)
                discovered.append(
                    {
                        "channel_id": cid,
                        "title": item.get("snippet", {}).get("title", ""),
                        "source": "youtube_search",
                        "avg_views": 0,
                    }
                )
        time.sleep(0.25)
    print(f"Discovered {len(discovered)} keyword-search channels")
    return discovered


def build_channel_seed_list() -> list[dict[str, Any]]:
    seeds = load_known_channels()
    seeds.extend(fetch_noxinfluencer_channels())

    youtubers_candidates = fetch_youtubers_me_candidates()
    candidate_cache = load_candidate_cache()
    resolved_candidates = resolve_youtubers_candidates(youtubers_candidates, candidate_cache)
    save_candidate_cache(resolved_candidates)

    for row in resolved_candidates:
        cid = row.get("channel_id")
        if not cid:
            continue
        seeds.append(
            {
                "channel_id": cid,
                "title": row.get("officialTitle") or row.get("name", ""),
                "source": row.get("source", "youtubers.me"),
                "sourceRank": row.get("sourceRank"),
                "avg_views": 0,
            }
        )

    existing_ids = {row["channel_id"] for row in seeds if row.get("channel_id")}
    if DISCOVERY_MODE or len(seeds) < TOP_CHANNEL_LIMIT:
        seeds.extend(discover_channels_by_youtube_search(existing_ids))

    deduped = []
    seen = set()
    for row in seeds:
        cid = row.get("channel_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        deduped.append(row)
    print(f"Seed channel IDs: {len(deduped)}")
    return deduped


def get_recent_video_ids(uploads_playlist_id: str) -> list[str]:
    if not uploads_playlist_id:
        return []
    data = youtube_api_get(
        "playlistItems",
        {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": LATEST_VIDEO_COUNT,
        },
    )
    if not data:
        return []
    return [
        item.get("contentDetails", {}).get("videoId", "")
        for item in data.get("items", [])
        if item.get("contentDetails", {}).get("videoId")
    ]


def get_video_details(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    unique_ids = list(dict.fromkeys([vid for vid in video_ids if vid]))
    for index in range(0, len(unique_ids), 50):
        batch = unique_ids[index : index + 50]
        data = youtube_api_get(
            "videos",
            {"part": "statistics,snippet", "id": ",".join(batch)},
        )
        if not data:
            continue
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            published_at = snippet.get("publishedAt", "")
            try:
                published_ts = int(
                    datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp()
                )
            except Exception:
                published_ts = 0
            thumbs = snippet.get("thumbnails", {})
            details[item["id"]] = {
                "videoId": item["id"],
                "title": snippet.get("title", ""),
                "thumbnailUrl": (
                    thumbs.get("maxres", {}).get("url")
                    or thumbs.get("high", {}).get("url")
                    or thumbs.get("medium", {}).get("url")
                    or ""
                ),
                "viewCount": parse_int(stats.get("viewCount")),
                "likeCount": parse_int(stats.get("likeCount")),
                "publishedAt": published_ts,
            }
        time.sleep(0.12)
    return details


def load_history() -> dict[str, Any]:
    return load_json_file(HISTORY_FILE, {})


def save_history(channels_data: list[dict[str, Any]]) -> None:
    history = load_history()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for channel in channels_data:
        cid = channel["id"]
        history.setdefault(cid, {})
        history[cid][today] = {
            "subscriberCount": channel["subscriberCount"],
            "videoCount": channel["videoCount"],
            "timestamp": int(time.time()),
        }
        dates = sorted(history[cid])
        for old_date in dates[:-35]:
            del history[cid][old_date]
    write_json_file(HISTORY_FILE, history)


def load_previous_ranks() -> dict[str, int]:
    return load_json_file(PREVIOUS_RANKS_FILE, {})


def save_current_ranks(channels_data: list[dict[str, Any]]) -> None:
    write_json_file(PREVIOUS_RANKS_FILE, {ch["id"]: ch["rank"] for ch in channels_data})


def compute_comparison(channel_id: str, history: dict[str, Any]) -> dict[str, int]:
    result = {}
    now = datetime.now(timezone.utc)
    channel_history = history.get(channel_id, {})
    for key, days in (
        ("yesterdaySubscribers", 1),
        ("weekAgoSubscribers", 7),
        ("monthAgoSubscribers", 30),
    ):
        date_key = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        if date_key in channel_history:
            result[key] = channel_history[date_key].get("subscriberCount")
    return result


def make_output_channel(
    seed: dict[str, Any],
    youtube_channel: dict[str, Any],
    history: dict[str, Any],
    previous_ranks: dict[str, int],
    now_ts: int,
    match_reason: str,
) -> dict[str, Any]:
    cid = youtube_channel["id"]
    snippet = youtube_channel.get("snippet", {})
    stats = youtube_channel.get("statistics", {})
    branding = youtube_channel.get("brandingSettings", {})
    content = youtube_channel.get("contentDetails", {})

    thumbnails = snippet.get("thumbnails", {})
    avatar_url = (
        thumbnails.get("high", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
        or thumbnails.get("default", {}).get("url")
        or ""
    )
    avatar_url = avatar_url.replace("/s88-", "/s240-").replace("=s88-", "=s240-")

    comparison = compute_comparison(cid, history)
    uploads_playlist = content.get("relatedPlaylists", {}).get("uploads", "")

    return {
        "id": cid,
        "title": snippet.get("title", "") or seed.get("title", ""),
        "avatarUrl": avatar_url,
        "bannerUrl": branding.get("image", {}).get("bannerExternalUrl", ""),
        "subscriberCount": parse_int(stats.get("subscriberCount")),
        "videoCount": parse_int(stats.get("videoCount")),
        "avgViews": parse_int(seed.get("avg_views")),
        "rank": 0,
        "previousRank": previous_ranks.get(cid, 0),
        "yesterdaySubscribers": comparison.get("yesterdaySubscribers"),
        "weekAgoSubscribers": comparison.get("weekAgoSubscribers"),
        "monthAgoSubscribers": comparison.get("monthAgoSubscribers"),
        "latestVideos": [],
        "lastUpdate": now_ts,
        "officialCountry": official_country(youtube_channel),
        "taiwanMatchReason": match_reason,
        "_uploadsPlaylist": uploads_playlist,
    }


def main() -> None:
    print("=== Taiwan YouTuber ranking update ===")
    print(f"UTC time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Taiwan channel filter: {VERIFY_OFFICIAL_TW}")

    seeds = build_channel_seed_list()
    if not seeds:
        print("No channel seeds available")
        return

    seeds_by_id = {row["channel_id"]: row for row in seeds}
    print(f"Refreshing {len(seeds_by_id)} official channel records")
    youtube_channels = batch_channels_list(list(seeds_by_id))
    history = load_history()
    previous_ranks = load_previous_ranks()
    now_ts = int(time.time())

    output_channels = []
    about_checks_used = 0
    for youtube_channel in youtube_channels:
        seed = seeds_by_id.get(youtube_channel["id"], {})
        allow_about_page = about_checks_used < ABOUT_PAGE_CHECK_LIMIT
        match_reason = taiwan_match_reason(
            youtube_channel,
            seed,
            allow_about_page=allow_about_page,
        )
        if allow_about_page and match_reason.startswith("youtube_about_"):
            about_checks_used += 1
        elif allow_about_page and not match_reason:
            about_checks_used += 1

        if VERIFY_OFFICIAL_TW and not match_reason:
            continue
        output_channels.append(
            make_output_channel(
                seed,
                youtube_channel,
                history,
                previous_ranks,
                now_ts,
                match_reason,
            )
        )

    if not output_channels:
        print("No official channel records available")
        return

    output_channels.sort(key=lambda row: row.get("subscriberCount", 0), reverse=True)
    output_channels = output_channels[:RANKING_POOL_SIZE]
    for rank, channel in enumerate(output_channels, 1):
        channel["rank"] = rank
        if not channel["previousRank"]:
            channel["previousRank"] = rank

    print(f"Fetching latest videos for top {LATEST_VIDEO_CHANNEL_LIMIT}")
    all_video_ids = []
    for channel in output_channels[:LATEST_VIDEO_CHANNEL_LIMIT]:
        ids = get_recent_video_ids(channel.get("_uploadsPlaylist", ""))
        channel["_latestVideoIds"] = ids
        all_video_ids.extend(ids)
        time.sleep(0.12)

    video_details = get_video_details(all_video_ids)
    for channel in output_channels:
        ordered_ids = channel.pop("_latestVideoIds", [])
        channel["latestVideos"] = [
            video_details[video_id]
            for video_id in ordered_ids
            if video_id in video_details
        ]
        channel.pop("_uploadsPlaylist", None)

    top_channels = output_channels[:TOP_CHANNEL_LIMIT]
    save_history(output_channels)
    save_current_ranks(output_channels)

    output = {
        "lastUpdate": now_ts,
        "channelCount": len(top_channels),
        "searchIndexCount": len(output_channels),
        "taiwanFilter": VERIFY_OFFICIAL_TW,
        "aboutPageChecksUsed": about_checks_used,
        "channels": top_channels,
        "searchIndex": output_channels,
    }
    write_json_file("data.json", output)
    write_json_file(
        CHANNELS_FILE,
        {
            "channels": [
                {
                    "id": channel["id"],
                    "title": channel["title"],
                    "officialCountry": channel.get("officialCountry", ""),
                    "taiwanMatchReason": channel.get("taiwanMatchReason", ""),
                }
                for channel in output_channels
            ]
        },
    )

    with_country = sum(1 for channel in output_channels if channel.get("officialCountry"))
    print(
        f"Done: top={len(top_channels)}, searchIndex={len(output_channels)}, "
        f"withOfficialCountry={with_country}, aboutChecks={about_checks_used}"
    )


if __name__ == "__main__":
    main()
