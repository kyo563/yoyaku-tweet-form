# yoyaku-tweet-form

予約投稿テキスト作成用の Streamlit アプリです。

## GitHub Actions: 最新動画状況データの取得

`.github/workflows/fetch-latest-video-status.yml` を追加しました。1時間ごと（毎時0分）または手動実行で、YouTube の最新動画状況を取得します。

### 取得データ
`data/latest_video_status.json`

- 動画ID
- タイトル
- 公開日時
- 視聴数
- 高評価数
- コメント数
- 動画URL
- 取得時刻(UTC)

### 事前設定（GitHub Secrets）
GitHub リポジトリの `Settings > Secrets and variables > Actions` に以下を設定してください。

- `YOUTUBE_API_KEY`: YouTube Data API v3 の API キー
- `YOUTUBE_CHANNEL_ID`: 取得対象チャンネルID

### 実行結果
ワークフロー実行後、`latest-video-status` という Artifact として JSON をダウンロードできます。
