#!/usr/bin/env python3
"""
台灣創作者Top100 - YouTube 數據抓取腳本
由 GitHub Actions 每 10 分鐘自動執行
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

API_KEY = os.environ.get('YOUTUBE_API_KEY', '')
BASE_URL = 'https://www.googleapis.com/youtube/v3'

# 台灣 Top YouTuber 種子名單（頻道 ID）
# 這些是已知的台灣熱門頻道，後續會自動擴充
SEED_CHANNEL_IDS = [
    # 會在首次執行後自動填入
]

# 搜尋關鍵字（用來發現新頻道）
SEARCH_KEYWORDS = [
    '台灣', 'Taiwan', '遊戲', '美食', 'Vlog', '音樂',
    '搞笑', '3C', '科技', '開箱', '美妝', '健身',
    '教育', '旅遊', '日常', '料理', '寵物', '動畫',
    '挑戰', 'Podcast',
]


def api_get(endpoint, params):
    """呼叫 YouTube API"""
    params['key'] = API_KEY
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"API error: {e}")
        return None


def search_taiwan_channels():
    """搜尋台灣頻道，收集候選頻道 ID"""
    channel_ids = set(SEED_CHANNEL_IDS)

    for keyword in SEARCH_KEYWORDS:
        params = {
            'part': 'snippet',
            'q': keyword,
            'type': 'channel',
            'regionCode': 'TW',
            'maxResults': 50,
            'order': 'viewCount',
        }
        data = api_get('search', params)
        if data and 'items' in data:
            for item in data['items']:
                cid = item['id']['channelId']
                channel_ids.add(cid)
        time.sleep(0.1)  # 避免太快

    print(f"找到 {len(channel_ids)} 個候選頻道")
    return list(channel_ids)


def batch_channels_list(channel_ids):
    """批次查詢頻道資料（每批最多 50 個）"""
    results = []
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i+50]
        params = {
            'part': 'snippet,statistics,brandingSettings',
            'id': ','.join(batch),
        }
        data = api_get('channels', params)
        if data and 'items' in data:
            results.extend(data['items'])
        time.sleep(0.1)
    return results


def get_latest_videos(channel_id, max_results=5):
    """取得頻道最新影片"""
    params = {
        'part': 'snippet',
        'channelId': channel_id,
        'maxResults': max_results,
        'order': 'date',
        'type': 'video',
    }
    data = api_get('search', params)
    if not data or 'items' not in data:
        return []

    video_ids = [item['id']['videoId'] for item in data['items']
                 if item['id'].get('videoId')]
    if not video_ids:
        return []

    # 取得影片統計
    params2 = {
        'part': 'statistics,snippet',
        'id': ','.join(video_ids),
    }
    data2 = api_get('videos', params2)
    if not data2 or 'items' not in data2:
        return []

    videos = []
    for item in data2['items']:
        videos.append({
            'videoId': item['id'],
            'title': item['snippet']['title'],
            'thumbnailUrl': item['snippet']['thumbnails'].get('medium', {}).get('url', ''),
            'viewCount': int(item['statistics'].get('viewCount', 0)),
            'likeCount': int(item['statistics'].get('likeCount', 0)),
            'publishedAt': item['snippet']['publishedAt'],
        })
    return videos


def get_historical_data(channel_id):
    """讀取歷史數據（從 data/history.json）"""
    history_file = 'data/history.json'
    if os.path.exists(history_file):
        with open(history_file, 'r') as f:
            history = json.load(f)
        return history.get(channel_id, {})
    return {}


def save_historical_data(channels_data):
    """儲存今日數據到歷史記錄"""
    history_file = 'data/history.json'
    os.makedirs('data', exist_ok=True)

    history = {}
    if os.path.exists(history_file):
        with open(history_file, 'r') as f:
            history = json.load(f)

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

    with open(history_file, 'w') as f:
        json.dump(history, f, ensure_ascii=False)


def compute_comparison(channel_id, history):
    """計算昨日/7日/30日比較"""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    from datetime import timedelta

    result = {}

    # 昨日
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    if yesterday in history:
        result['yesterdaySubscribers'] = history[yesterday].get('subscriberCount')

    # 7日前
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
    if week_ago in history:
        result['weekAgoSubscribers'] = history[week_ago].get('subscriberCount')

    # 30日前
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
    if month_ago in history:
        result['monthAgoSubscribers'] = history[month_ago].get('subscriberCount')

    return result


def main():
    print("=== 台灣創作者Top100 數據更新 ===")
    print(f"時間：{datetime.now(timezone.utc).isoformat()}")

    # 載入已知頻道列表
    channels_file = 'data/channels.json'
    os.makedirs('data', exist_ok=True)

    known_ids = set(SEED_CHANNEL_IDS)
    if os.path.exists(channels_file):
        with open(channels_file, 'r') as f:
            existing = json.load(f)
            for ch in existing.get('channels', []):
                known_ids.add(ch['id'])

    # 搜尋新頻道（每週日執行，或首次執行時）
    today = datetime.now(timezone.utc).strftime('%A')
    if today == 'Sunday' or len(known_ids) < 100:
        print("執行頻道搜尋...")
        new_ids = search_taiwan_channels()
        known_ids.update(new_ids)
        print(f"總計 {len(known_ids)} 個頻道")

    channel_ids = list(known_ids)

    # 批次查詢頻道資料
    print(f"查詢 {len(channel_ids)} 個頻道...")
    channels_raw = batch_channels_list(channel_ids)
    print(f"取得 {len(channels_raw)} 個頻道資料")

    # 過濾台灣頻道（country=TW 或中文內容）
    tw_channels = []
    for ch in channels_raw:
        snippet = ch.get('snippet', {})
        country = snippet.get('country', '')
        # 接受 TW 或沒有設定國家（可能是台灣頻道）
        if country in ('TW', ''):
            tw_channels.append(ch)

    # 依訂閱數排序
    tw_channels.sort(
        key=lambda x: int(x.get('statistics', {}).get('subscriberCount', 0)),
        reverse=True
    )

    # 取 Top 100
    top_channels = tw_channels[:100]

    # 讀取歷史數據
    history = {}
    history_file = 'data/history.json'
    if os.path.exists(history_file):
        with open(history_file, 'r') as f:
            history = json.load(f)

    # 組裝最終輸出
    output_channels = []
    now_ts = int(time.time())

    for rank, ch in enumerate(top_channels, 1):
        cid = ch['id']
        snippet = ch.get('snippet', {})
        stats = ch.get('statistics', {})
        branding = ch.get('brandingSettings', {})

        avatar_url = snippet.get('thumbnails', {}).get('default', {}).get('url', '')
        avatar_url = avatar_url.replace('/s88-', '/s240-')  # 取大圖
        banner_url = branding.get('image', {}).get('bannerExternalUrl', '')

        sub_count = int(stats.get('subscriberCount', 0))
        vid_count = int(stats.get('videoCount', 0))

        # 歷史比較
        ch_history = history.get(cid, {})
        comparison = compute_comparison(cid, ch_history)

        # 最新影片
        latest_videos = []
        try:
            latest_videos = get_latest_videos(cid, 3)
        except Exception as e:
            print(f"取得影片失敗 {cid}: {e}")

        output_channels.append({
            'id': cid,
            'title': snippet.get('title', ''),
            'avatarUrl': avatar_url,
            'bannerUrl': banner_url,
            'subscriberCount': sub_count,
            'videoCount': vid_count,
            'rank': rank,
            'previousRank': rank,  # 首次執行時排名不變
            'yesterdaySubscribers': comparison.get('yesterdaySubscribers'),
            'weekAgoSubscribers': comparison.get('weekAgoSubscribers'),
            'monthAgoSubscribers': comparison.get('monthAgoSubscribers'),
            'latestVideos': latest_videos,
            'lastUpdate': now_ts,
        })

    # 儲存歷史數據
    save_historical_data(output_channels)

    # 產出 JSON
    output = {
        'lastUpdate': now_ts,
        'channelCount': len(output_channels),
        'channels': output_channels,
    }

    # 寫入 data.json（GitHub Pages 會提供這個檔案）
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 同時備份頻道列表
    with open(channels_file, 'w', encoding='utf-8') as f:
        json.dump({'channels': [{'id': c['id'], 'title': c['title']} for c in output_channels]},
                  f, ensure_ascii=False, indent=2)

    print(f"完成！共 {len(output_channels)} 個頻道，已輸出 data.json")


if __name__ == '__main__':
    main()
