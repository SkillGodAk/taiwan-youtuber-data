#!/usr/bin/env python3
"""
台灣創作者Top100 - YouTube 數據抓取腳本
由 GitHub Actions 定期自動執行

使用 Noxinfluencer API 取得排名 + avgViews
使用 YouTube Data API v3 取得精確的訂閱數、影片數等
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get('YOUTUBE_API_KEY', '')
BASE_URL = 'https://www.googleapis.com/youtube/v3'
LATEST_VIDEO_CHANNEL_LIMIT = int(os.environ.get('LATEST_VIDEO_CHANNEL_LIMIT', '20'))
LATEST_VIDEO_COUNT = int(os.environ.get('LATEST_VIDEO_COUNT', '3'))
TOP_CHANNEL_LIMIT = int(os.environ.get('TOP_CHANNEL_LIMIT', '100'))
RANKING_POOL_SIZE = int(os.environ.get('RANKING_POOL_SIZE', '500'))
DISCOVERY_MODE = os.environ.get('DISCOVERY_MODE', '').lower() in ('1', 'true', 'yes')

NOXINFLUENCER_URL = 'https://www.noxinfluencer.com/ws/rank/youtube/kol'
CHANNELS_FILE = 'data/channels.json'
DISCOVERY_KEYWORDS = [
    '台灣 YouTuber',
    '台灣 vlog',
    '台灣 美食',
    '台灣 遊戲',
    '台灣 旅遊',
    '台灣 開箱',
    '台灣 音樂',
    '台灣 寵物',
    '台灣 科技',
    '台灣 搞笑',
    'Taiwan YouTuber',
    'Taiwan vlog',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
}


def fetch_noxinfluencer_page(page_num, page_size=100):
    """從 Noxinfluencer 取得單頁排名資料。"""
    params = urllib.parse.urlencode({
        'country': 'TW',
        'rankType': 'followers',
        'interval': 'weekly',
        'pageNum': page_num,
        'pageSize': page_size,
    })
    req = urllib.request.Request(f"{NOXINFLUENCER_URL}?{params}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Noxinfluencer API 錯誤: {e}")
        return []

    # 解析回應結構
    rows = []
    if isinstance(data, dict):
        rows = data.get('retDataList', data.get('data', data.get('rows', data.get('list', []))))
    elif isinstance(data, list):
        rows = data

    if not rows:
        print(f"Noxinfluencer 回應格式異常: keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
        # 嘗試印出部分內容幫助 debug
        sample = json.dumps(data, ensure_ascii=False)[:500]
        print(f"回應內容前 500 字: {sample}")
        return []

    return rows


def fetch_noxinfluencer_channels(limit=RANKING_POOL_SIZE):
    """從 Noxinfluencer 取得台灣 YouTube 排名頻道。"""
    print(f"正在從 Noxinfluencer 取得前 {limit} 名...")
    rows = []
    page_size = 100
    max_pages = max(1, (limit + page_size - 1) // page_size)

    for page_num in range(1, max_pages + 1):
        page_rows = fetch_noxinfluencer_page(page_num, page_size)
        if not page_rows:
            if page_num == 1:
                return []
            print(f"第 {page_num} 頁沒有資料，停止延伸索引抓取")
            break
        rows.extend(page_rows)
        if len(rows) >= limit:
            break
        time.sleep(0.3)

    channels = []
    seen_ids = set()
    for i, row in enumerate(rows[:limit]):
        # Noxinfluencer 欄位名稱可能不同，嘗試常見的 key
        channel_id = (
            row.get('channelId') or row.get('id') or
            row.get('youtube_id') or row.get('channel_id') or ''
        )
        title = row.get('title') or row.get('name') or row.get('channelName') or ''
        avatar = (
            row.get('avatar') or row.get('thumbnail') or
            row.get('avatarUrl') or row.get('img') or ''
        )
        avg_views = row.get('avgViews') or row.get('avg_views') or row.get('avgView') or 0

        # 確保 avg_views 是整數
        try:
            avg_views = int(avg_views)
        except (ValueError, TypeError):
            avg_views = 0

        if channel_id and channel_id not in seen_ids:
            seen_ids.add(channel_id)
            channels.append({
                'nox_rank': len(channels) + 1,
                'channel_id': channel_id,
                'title': title,
                'avatar': avatar,
                'avg_views': avg_views,
            })

    print(f"從 Noxinfluencer 取得 {len(channels)} 個頻道")
    if channels:
        sample = channels[0]
        print(f"範例頻道: id={sample['channel_id']}, title={sample['title']}, avgViews={sample['avg_views']}")
    return channels


def youtube_api_get(endpoint, params):
    """呼叫 YouTube Data API v3"""
    params['key'] = API_KEY
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"YouTube API 錯誤 ({endpoint}): {e}")
        return None


def load_known_channels():
    """讀取既有搜尋索引，正常排程靠這份清單校正排名。"""
    if not os.path.exists(CHANNELS_FILE):
        return []

    try:
        with open(CHANNELS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    channels = []
    for row in data.get('channels', []):
        cid = row.get('id') or row.get('channel_id')
        if cid:
            channels.append({
                'nox_rank': row.get('rank') or len(channels) + 1,
                'channel_id': cid,
                'title': row.get('title', ''),
                'avatar': row.get('avatarUrl', ''),
                'avg_views': row.get('avgViews', 0),
            })
    return channels


def discover_channels_by_youtube_search(existing_ids, limit=RANKING_POOL_SIZE):
    """低頻 discovery 用 search.list 補候選池，不在一般 10 分鐘排程大量使用。"""
    discovered = []
    seen = set(existing_ids)

    for keyword in DISCOVERY_KEYWORDS:
        if len(seen) >= limit:
            break

        print(f"Discovery 搜尋：{keyword}")
        data = youtube_api_get('search', {
            'part': 'snippet',
            'q': keyword,
            'type': 'channel',
            'regionCode': 'TW',
            'order': 'viewCount',
            'maxResults': 50,
        })
        if not data:
            continue

        for item in data.get('items', []):
            cid = item.get('id', {}).get('channelId', '')
            if not cid or cid in seen:
                continue
            snippet = item.get('snippet', {})
            seen.add(cid)
            discovered.append({
                'nox_rank': len(seen),
                'channel_id': cid,
                'title': snippet.get('title', ''),
                'avatar': snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
                'avg_views': 0,
            })
            if len(seen) >= limit:
                break

        time.sleep(0.3)

    print(f"Discovery 新增 {len(discovered)} 個候選頻道")
    return discovered


def batch_channels_list(channel_ids):
    """批次查詢頻道資料（YouTube API 每批最多 50 個）"""
    results = []
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i + 50]
        params = {
            'part': 'snippet,statistics,brandingSettings,contentDetails',
            'id': ','.join(batch),
        }
        data = youtube_api_get('channels', params)
        if data and 'items' in data:
            results.extend(data['items'])
        time.sleep(0.15)  # 避免 rate limit
    return results


def get_recent_video_ids(uploads_playlist_id, max_results=LATEST_VIDEO_COUNT):
    """用 uploads playlist 取得最新影片 ID。比 search.list 省很多 quota。"""
    if not uploads_playlist_id:
        return []

    params = {
        'part': 'contentDetails',
        'playlistId': uploads_playlist_id,
        'maxResults': max_results,
    }
    data = youtube_api_get('playlistItems', params)
    if not data or 'items' not in data:
        return []

    return [
        item.get('contentDetails', {}).get('videoId', '')
        for item in data['items']
        if item.get('contentDetails', {}).get('videoId')
    ]


def get_video_details(video_ids):
    """批次取得影片資訊，回傳 videoId -> video info。"""
    details = {}
    unique_ids = list(dict.fromkeys([vid for vid in video_ids if vid]))

    for i in range(0, len(unique_ids), 50):
        batch = unique_ids[i:i + 50]
        params = {
            'part': 'statistics,snippet',
            'id': ','.join(batch),
        }
        data = youtube_api_get('videos', params)
        if not data or 'items' not in data:
            continue

        for item in data['items']:
            snippet = item.get('snippet', {})
            stats = item.get('statistics', {})
            published_at = snippet.get('publishedAt', '')
            try:
                dt = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
                ts = int(dt.timestamp())
            except Exception:
                ts = 0

            details[item['id']] = {
                'videoId': item['id'],
                'title': snippet.get('title', ''),
                'thumbnailUrl': (
                    snippet.get('thumbnails', {}).get('maxres', {}).get('url', '') or
                    snippet.get('thumbnails', {}).get('high', {}).get('url', '') or
                    snippet.get('thumbnails', {}).get('medium', {}).get('url', '')
                ),
                'viewCount': int(stats.get('viewCount', 0)),
                'likeCount': int(stats.get('likeCount', 0)),
                'publishedAt': ts,
            }

        time.sleep(0.15)

    return details


def load_history():
    """讀取歷史數據"""
    history_file = 'data/history.json'
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_history(channels_data):
    """儲存今日數據到歷史記錄"""
    history_file = 'data/history.json'
    os.makedirs('data', exist_ok=True)

    history = load_history()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    for ch in channels_data:
        cid = ch['id']
        if cid not in history:
            history[cid] = {}
        history[cid][today] = {
            'subscriberCount': ch['subscriberCount'],
            'videoCount': ch['videoCount'],
            'timestamp': int(time.time()),
        }
        # 只保留最近 35 天
        dates = sorted(history[cid].keys())
        if len(dates) > 35:
            for old_date in dates[:-35]:
                del history[cid][old_date]

    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False)


def load_previous_ranks():
    """讀取上一次的排名，用於計算排名變化"""
    prev_file = 'data/previous_ranks.json'
    if os.path.exists(prev_file):
        with open(prev_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_current_ranks(channels_data):
    """儲存本次排名，供下次比較"""
    prev_file = 'data/previous_ranks.json'
    os.makedirs('data', exist_ok=True)
    ranks = {ch['id']: ch['rank'] for ch in channels_data}
    with open(prev_file, 'w', encoding='utf-8') as f:
        json.dump(ranks, f, ensure_ascii=False)


def compute_comparison(channel_id, history):
    """計算昨日/7日/30日比較"""
    result = {}
    now = datetime.now(timezone.utc)

    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    if yesterday in history:
        result['yesterdaySubscribers'] = history[yesterday].get('subscriberCount')

    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    if week_ago in history:
        result['weekAgoSubscribers'] = history[week_ago].get('subscriberCount')

    month_ago = (now - timedelta(days=30)).strftime('%Y-%m-%d')
    if month_ago in history:
        result['monthAgoSubscribers'] = history[month_ago].get('subscriberCount')

    return result


def main():
    print("=== 台灣 YouTuber 排行榜數據更新 ===")
    print(f"時間：{datetime.now(timezone.utc).isoformat()}")

    # 1. 取得候選頻道池。Noxinfluencer 可用時優先使用；失敗時用既有索引。
    nox_channels = fetch_noxinfluencer_channels()
    if not nox_channels:
        print("無法從 Noxinfluencer 取得數據，改用既有頻道索引")
        nox_channels = load_known_channels()

    if DISCOVERY_MODE or len(nox_channels) < TOP_CHANNEL_LIMIT:
        existing_ids = [channel['channel_id'] for channel in nox_channels]
        nox_channels.extend(
            discover_channels_by_youtube_search(existing_ids, RANKING_POOL_SIZE)
        )

    # 去重並限制候選池大小
    deduped_channels = []
    seen_ids = set()
    for channel in nox_channels:
        cid = channel.get('channel_id')
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        deduped_channels.append(channel)
        if len(deduped_channels) >= RANKING_POOL_SIZE:
            break
    nox_channels = deduped_channels

    if not nox_channels:
        print("沒有候選頻道，終止")
        return

    # 建立 channel_id -> nox_data 的對照表
    nox_map = {c['channel_id']: c for c in nox_channels}
    channel_ids = [c['channel_id'] for c in nox_channels]

    # 2. 用 YouTube API 批次查詢頻道詳細資料
    print(f"正在用 YouTube API 查詢 {len(channel_ids)} 個頻道...")
    yt_channels = batch_channels_list(channel_ids)
    print(f"YouTube API 回傳 {len(yt_channels)} 個頻道")

    # 建立 YouTube 數據對照表
    yt_map = {}
    for ch in yt_channels:
        cid = ch['id']
        yt_map[cid] = ch

    # 3. 讀取歷史數據和上次排名
    history = load_history()
    previous_ranks = load_previous_ranks()

    # 4. 組裝最終輸出，稍後再依 YouTube 訂閱數排序
    output_channels = []
    now_ts = int(time.time())

    for nox_ch in nox_channels:
        cid = nox_ch['channel_id']
        yt_ch = yt_map.get(cid)

        if not yt_ch:
            # YouTube API 找不到這個頻道，跳過
            print(f"  跳過 {cid}（YouTube API 未回傳）")
            continue

        snippet = yt_ch.get('snippet', {})
        stats = yt_ch.get('statistics', {})
        branding = yt_ch.get('brandingSettings', {})
        content = yt_ch.get('contentDetails', {})

        # 頭像：取較大尺寸
        avatar_url = snippet.get('thumbnails', {}).get('default', {}).get('url', '')
        avatar_url = avatar_url.replace('/s88-', '/s240-').replace('=s88-', '=s240-')
        if not avatar_url and nox_ch.get('avatar'):
            avatar_url = nox_ch['avatar']

        banner_url = branding.get('image', {}).get('bannerExternalUrl', '')

        sub_count = int(stats.get('subscriberCount', 0))
        vid_count = int(stats.get('videoCount', 0))
        avg_views = nox_ch.get('avg_views', 0)

        # 歷史比較
        ch_history = history.get(cid, {})
        comparison = compute_comparison(cid, ch_history)

        uploads_playlist = (
            content.get('relatedPlaylists', {}).get('uploads', '')
            if isinstance(content, dict) else ''
        )

        title = snippet.get('title', '') or nox_ch.get('title', '')

        output_channels.append({
            'id': cid,
            'title': title,
            'avatarUrl': avatar_url,
            'bannerUrl': banner_url,
            'subscriberCount': sub_count,
            'videoCount': vid_count,
            'avgViews': avg_views,
            'rank': 0,
            'previousRank': previous_ranks.get(cid, 0),
            'yesterdaySubscribers': comparison.get('yesterdaySubscribers'),
            'weekAgoSubscribers': comparison.get('weekAgoSubscribers'),
            'monthAgoSubscribers': comparison.get('monthAgoSubscribers'),
            'latestVideos': [],
            'lastUpdate': now_ts,
            '_uploadsPlaylist': uploads_playlist,
        })

    if not output_channels:
        print("沒有成功組裝任何頻道數據，終止")
        return

    # 依公開 YouTube API 訂閱數重新排序，首頁排名才會跟今日訂閱數一致。
    output_channels.sort(key=lambda c: c.get('subscriberCount', 0), reverse=True)
    for rank, channel in enumerate(output_channels, 1):
        channel['rank'] = rank
        if not channel['previousRank']:
            channel['previousRank'] = rank

    # 最新影片只抓前 N 名，兼顧「有展示」與每日 YouTube API 配額。
    print(f"抓取前 {LATEST_VIDEO_CHANNEL_LIMIT} 名最新影片，每頻道 {LATEST_VIDEO_COUNT} 支...")
    all_video_ids = []
    for channel in output_channels[:LATEST_VIDEO_CHANNEL_LIMIT]:
        video_ids = get_recent_video_ids(channel.get('_uploadsPlaylist', ''))
        channel['_latestVideoIds'] = video_ids
        all_video_ids.extend(video_ids)
        time.sleep(0.15)

    video_details = get_video_details(all_video_ids)
    for channel in output_channels:
        ordered_ids = channel.pop('_latestVideoIds', [])
        channel['latestVideos'] = [
            video_details[video_id]
            for video_id in ordered_ids
            if video_id in video_details
        ]
        channel.pop('_uploadsPlaylist', None)

    top_channels = output_channels[:TOP_CHANNEL_LIMIT]

    # 5. 儲存歷史和排名
    save_history(output_channels)
    save_current_ranks(output_channels)

    # 6. 產出 JSON
    output = {
        'lastUpdate': now_ts,
        'channelCount': len(top_channels),
        'searchIndexCount': len(output_channels),
        'channels': top_channels,
        'searchIndex': output_channels,
    }

    os.makedirs('data', exist_ok=True)

    # 寫入 data.json（GitHub Pages 提供）
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 備份頻道列表
    with open('data/channels.json', 'w', encoding='utf-8') as f:
        json.dump(
            {'channels': [{'id': c['id'], 'title': c['title']} for c in output_channels]},
            f, ensure_ascii=False, indent=2,
        )

    print(f"\n完成！首頁 {len(top_channels)} 個，搜尋索引 {len(output_channels)} 個，已輸出 data.json")

    # 統計 avgViews 覆蓋率
    with_avg = sum(1 for c in output_channels if c.get('avgViews', 0) > 0)
    print(f"avgViews 覆蓋率: {with_avg}/{len(output_channels)}")


if __name__ == '__main__':
    main()
