import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def request_json(path: str, params: dict[str, str]) -> dict:
    query = urlencode(params)
    url = f"{YOUTUBE_API_BASE}/{path}?{query}"
    with urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_latest_video_id(api_key: str, channel_id: str) -> str:
    data = request_json(
        "search",
        {
            "key": api_key,
            "channelId": channel_id,
            "part": "snippet",
            "maxResults": "1",
            "order": "date",
            "type": "video",
        },
    )
    items = data.get("items", [])
    if not items:
        raise RuntimeError("最新動画が見つかりませんでした。チャンネルIDを確認してください。")
    video_id = items[0].get("id", {}).get("videoId")
    if not video_id:
        raise RuntimeError("動画IDの取得に失敗しました。")
    return video_id


def fetch_video_status(api_key: str, video_id: str) -> dict:
    data = request_json(
        "videos",
        {
            "key": api_key,
            "id": video_id,
            "part": "snippet,statistics",
        },
    )
    items = data.get("items", [])
    if not items:
        raise RuntimeError("動画詳細の取得に失敗しました。")

    item = items[0]
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})

    return {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "published_at": snippet.get("publishedAt", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "url": f"https://youtu.be/{video_id}",
        "view_count": int(statistics.get("viewCount", 0)),
        "like_count": int(statistics.get("likeCount", 0)),
        "comment_count": int(statistics.get("commentCount", 0)),
    }


def main() -> int:
    api_key = os.getenv("YOUTUBE_API_KEY")
    channel_id = os.getenv("YOUTUBE_CHANNEL_ID")
    output_path = Path(os.getenv("OUTPUT_PATH", "data/latest_video_status.json"))

    if not api_key:
        print("ERROR: YOUTUBE_API_KEY が設定されていません。", file=sys.stderr)
        return 1
    if not channel_id:
        print("ERROR: YOUTUBE_CHANNEL_ID が設定されていません。", file=sys.stderr)
        return 1

    status = fetch_video_status(api_key, fetch_latest_video_id(api_key, channel_id))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"最新動画情報を取得しました: {status['title']}")
    print(f"出力先: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
