import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import streamlit as st
from streamlit.components.v1 import html
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest

# ==============================
# 設定
# ==============================

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
SPREADSHEET_ID = "1t34GoYFFHJdCsIjvbSGLEfD7W-cfeDgyAQoh9_u-oUU"  # 共有シートID
URL_UNITS = 24  # X(Twitter) URL固定長（安全側24）

# ==============================
# データ構造
# ==============================

@dataclass
class Template:
    id: str
    name: str
    body: str
    is_default: bool = False


@dataclass
class Video:
    video_id: str
    title: str
    description: str
    publish_at_utc: datetime

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


# ==============================
# テキスト処理
# ==============================

def count_units(text: str) -> int:
    total = 0
    for ch in text:
        w = unicodedata.east_asian_width(ch)
        total += 2 if w in ("F", "W", "A") else 1
    return total


def truncate_to_limit(text: str, max_units: int = 280) -> str:
    """
    汎用トリム関数。デフォルトはツイート上限の 280unit。
    snippet 用など、別上限をかけたい場合は引数で上書きする。
    """
    result_chars, length = [], 0
    for ch in text:
        add = 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
        if length + add > max_units:
            break
        result_chars.append(ch)
        length += add
    truncated = "".join(result_chars)
    if truncated != text:
        truncated += "…"
    return truncated


def count_units_breakdown(text: str) -> tuple[int, int, int]:
    """
    本文ユニット, URLユニット(=URL_UNITS×本数), URL本数 を返す
    """
    url_pattern = re.compile(r"https?://\S+")
    body_units, url_count, pos = 0, 0, 0
    for m in url_pattern.finditer(text):
        body_units += count_units(text[pos:m.start()])
        url_count += 1
        pos = m.end()
    body_units += count_units(text[pos:])
    return body_units, URL_UNITS * url_count, url_count


def extract_snippet(description: str, max_units: int = 200) -> str:
    """
    概要欄からURL/見出し行を除いた短文を生成。
    ツイート本文中で「概要欄由来として使ってよい予算」は 200unit までとする。
    改行はそのまま保持して差し込む。
    """
    lines = description.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        # 空行 / # 始まり / URL を含む行はスキップ
        if not s or s.startswith("#") or re.search(r"https?://", s):
            continue
        cleaned.append(s)
    # ここだけ 200unit 上限を適用（改行は "\n" で保持）
    return truncate_to_limit("\n".join(cleaned), max_units=max_units)


def format_publish_at(dt: datetime, tz_name: str = "Asia/Tokyo") -> str:
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    return local.strftime("%Y/%m/%d %H:%M")


def format_publish_at_with_weekday(dt: datetime, tz_name: str = "Asia/Tokyo") -> str:
    """
    yyyy/mm/dd(曜) hh:mm 形式で返す。曜は日本語一文字。
    例: 2025/11/14(金) 21:00
    """
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    wd = ["月", "火", "水", "木", "金", "土", "日"][local.weekday()]
    date_str = local.strftime("%Y/%m/%d")
    time_str = local.strftime("%H:%M")
    return f"{date_str}({wd}) {time_str}"


def format_publish_at_pretty(dt: datetime, tz_name: str = "Asia/Tokyo") -> str:
    """
    m月d日(曜) 形式で返す。曜は日本語の一文字（例：水）。
    """
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    wd = ["月", "火", "水", "木", "金", "土", "日"][local.weekday()]
    return f"{local.month}月{local.day}日({wd})"


# URL/title/publish_at は削らない仕様だが、汎用関数として残しておく
def ensure_url_and_limit(text: str, url: str, max_units: int = 280) -> str:
    if not url or url not in text:
        return text if count_units(text) <= max_units else truncate_to_limit(text, max_units=max_units)
    idx = text.rfind(url)
    prefix = text[:idx]
    if count_units(prefix) + URL_UNITS <= max_units:
        return prefix + url
    allowed = max_units - URL_UNITS
    if allowed <= 0:
        return url
    return truncate_to_limit(prefix, max_units=allowed) + url


def prioritize_and_fit(
    raw: str,
    url_text: str,
    title_text: str,
    snippet_text: str,
    publish_text: str,
    max_units: int = 280,
) -> str:
    # 現行仕様では使わない（URL/title/publish_atを優先的に削るロジックを封印）
    return raw


def build_tweet_from_template(template_body: str, video: Video, snippet: str, max_units: int = 280) -> str:
    """
    テンプレに差し込み後、そのまま返す。
    {url} / {title} / {publish_at} は常に全文を挿入し、
    {snippet} は extract_snippet 側で 200unit 以内に調整済みとする。
    280unit を超えてもここでは削らない（カウンタで警告のみ）。
    """
    publish_at_pretty = format_publish_at_pretty(video.publish_at_utc)
    url = video.url
    title = video.title

    raw = template_body.format(
        title=title,
        url=url,
        snippet=snippet,
        publish_at=publish_at_pretty,
    )
    return raw


# ==============================
# Google OAuth
# ==============================

def get_client_config() -> dict:
    info = st.secrets["google_oauth"]
    return {
        "web": {
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "auth_uri": info.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": info.get("token_uri", "https://oauth2.googleapis.com/token"),
            "redirect_uris": [info["redirect_uri"]],
        }
    }


def create_flow() -> Flow:
    flow = Flow.from_client_config(
        client_config=get_client_config(),
        scopes=SCOPES,
        redirect_uri=st.secrets["google_oauth"]["redirect_uri"],
    )
    return flow


def handle_oauth_callback():
    params = st.experimental_get_query_params()
    if "code" not in params:
        return
    code = params.get("code", [None])[0]
    if not code:
        return
    flow = create_flow()
    flow.fetch_token(code=code)
    st.session_state["google_creds"] = flow.credentials
    st.experimental_set_query_params()
    st.rerun()


def start_google_oauth():
    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    st.markdown(f"[Googleアカウントで連携する]({auth_url})")


def ensure_valid_creds(creds: Optional[Credentials]) -> Optional[Credentials]:
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            st.session_state["google_creds"] = creds
        except Exception:
            pass
    return creds


# ==============================
# YouTube API
# ==============================

def fetch_scheduled_videos(creds: Credentials) -> List[Video]:
    """
    予約投稿の通常動画/ショート ＋
    公開済みかつ配信前のライブ配信（ライブ枠）をまとめて取得する。

    - 予約投稿動画:
        status.publishAt があり、
        privacyStatus == "private" かつ publishAt が現在より未来のもの
    - ライブ枠:
        search.list(eventType="upcoming", type="video", channelId=...) で videoId を取得し、
        videos.list(..., part="snippet,liveStreamingDetails,status") で
        liveStreamingDetails.scheduledStartTime が現在より未来、
        かつ privacyStatus が "public" または "unlisted" のもの。
    """
    creds = ensure_valid_creds(creds)
    youtube = build("youtube", "v3", credentials=creds)
    now = datetime.now(timezone.utc)

    videos_uploads: List[Video] = []
    videos_lives: List[Video] = []

    # 自チャンネルの uploads プレイリストID と channelId を取得
    channels_resp = youtube.channels().list(
        part="id,contentDetails",
        mine=True,
    ).execute()
    items = channels_resp.get("items", [])
    if not items:
        return []

    channel_id = items[0]["id"]
    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # ==============================
    # 1) 予約投稿動画（通常動画/ショート）
    # ==============================
    video_ids: List[str] = []
    page_token = None
    while True:
        pl = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        video_ids += [
            it["contentDetails"]["videoId"]
            for it in pl.get("items", [])
        ]
        page_token = pl.get("nextPageToken")
        if not page_token:
            break

    if video_ids:
        for i in range(0, len(video_ids), 50):
            resp = youtube.videos().list(
                part="snippet,status",
                id=",".join(video_ids[i:i + 50]),
            ).execute()
            for item in resp.get("items", []):
                status = item.get("status", {})
                snip = item.get("snippet", {})

                publish_at_str = status.get("publishAt")
                if not publish_at_str:
                    # 即時公開やアーカイブなど publishAt が無いものは除外
                    continue

                try:
                    publish_dt = datetime.fromisoformat(
                        publish_at_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue

                # 「公開日時指定の予約投稿」だけを拾う
                if status.get("privacyStatus") != "private":
                    continue
                if publish_dt <= now:
                    continue

                videos_uploads.append(
                    Video(
                        video_id=item["id"],
                        title=snip.get("title", ""),
                        description=snip.get("description", ""),
                        publish_at_utc=publish_dt,
                    )
                )

    # ==============================
    # 2) 公開済み＆配信前のライブ枠（upcoming）
    # ==============================
    live_ids: List[str] = []
    page_token = None
    while True:
        resp = youtube.search().list(
            part="id",
            channelId=channel_id,
            eventType="upcoming",
            type="video",
            order="date",
            maxResults=50,
            pageToken=page_token,
        ).execute()

        for item in resp.get("items", []):
            id_obj = item.get("id", {})
            vid = id_obj.get("videoId")
            if vid:
                live_ids.append(vid)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if live_ids:
        for i in range(0, len(live_ids), 50):
            resp = youtube.videos().list(
                part="snippet,liveStreamingDetails,status",
                id=",".join(live_ids[i:i + 50]),
            ).execute()
            for item in resp.get("items", []):
                status = item.get("status", {})
                snip = item.get("snippet", {})
                lsd = item.get("liveStreamingDetails", {}) or {}

                sched_str = lsd.get("scheduledStartTime")
                if not sched_str:
                    continue
                try:
                    sched_dt = datetime.fromisoformat(
                        sched_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue

                # これから配信される枠だけ
                if sched_dt <= now:
                    continue

                # URL公開済み（public / unlisted）のみ対象
                if status.get("privacyStatus") not in ("public", "unlisted"):
                    continue

                videos_lives.append(
                    Video(
                        video_id=item["id"],
                        title=snip.get("title", ""),
                        description=snip.get("description", ""),
                        publish_at_utc=sched_dt,
                    )
                )

    # ==============================
    # 3) マージ & ソート（video_id で重複排除）
    # ==============================
    videos_by_id: dict[str, Video] = {}
    for v in videos_uploads + videos_lives:
        videos_by_id[v.video_id] = v

    videos: List[Video] = list(videos_by_id.values())
    videos.sort(key=lambda v: v.publish_at_utc)
    return videos


# ==============================
# Google Sheets（テンプレ）
# ==============================

def default_templates() -> List[Template]:
    return [
        Template(
            id="1",
            name="シンプルなお知らせ",
            body="【新着】{title}\n\n{snippet}\n\n▼動画はこちら\n{url}",
            is_default=True
        ),
        Template(
            id="2",
            name="丁寧めなお知らせ",
            body="本日 {publish_at} に動画を公開予定です。\n\n{snippet}\n\n{url}",
            is_default=False
        ),
    ]


def load_templates_from_sheets(creds: Credentials) -> List[Template]:
    if not SPREADSHEET_ID:
        return default_templates()
    creds = ensure_valid_creds(creds)
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Templates!A2:D",
    ).execute()
    values = result.get("values", [])
    if not values:
        return default_templates()
    out: List[Template] = []
    for row in values:
        t_id = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else ""
        body = row[2] if len(row) > 2 else ""
        is_default = (str(row[3]).upper() == "TRUE") if len(row) > 3 else False
        if t_id and name and body:
            out.append(Template(id=t_id, name=name, body=body, is_default=is_default))
    return out or default_templates()


def save_templates_to_sheets(creds: Credentials, templates: List[Template]) -> None:
    if not SPREADSHEET_ID:
        return
    creds = ensure_valid_creds(creds)
    service = build("sheets", "v4", credentials=creds)
    values = [[t.id, t.name, t.body, "TRUE" if t.is_default else "FALSE"] for t in templates]
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range="Templates!A2:D"
    ).execute()
    if values:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range="Templates!A2:D",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()


def append_template_to_sheets(creds: Credentials, template: Template) -> None:
    if not SPREADSHEET_ID:
        return
    creds = ensure_valid_creds(creds)
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Templates!A:D",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[
            template.id,
            template.name,
            template.body,
            "TRUE" if template.is_default else "FALSE"
        ]]},
    ).execute()


def next_template_id(existing: List[Template]) -> str:
    nums = []
    for t in existing:
        try:
            nums.append(int(t.id))
        except Exception:
            pass
    return str(max(nums) + 1) if nums else uuid.uuid4().hex[:8]


# ==============================
# アプリ本体
# ==============================

def main():
    st.set_page_config(page_title="予約投稿作成フォーム", layout="wide")
    st.title("📝 予約投稿作成フォーム（YouTube×X）")

    st.markdown(
        "<style>div[data-testid='stExpander']{margin-bottom:0.75rem}</style>",
        unsafe_allow_html=True,
    )

    # OAuth コールバック処理
    handle_oauth_callback()

    # セッション初期化
    st.session_state.setdefault("google_creds", None)
    st.session_state.setdefault("videos", [])
    st.session_state.setdefault("templates", default_templates())
    st.session_state.setdefault("tweet_text", "")
    st.session_state.setdefault("tmpl_picker", None)          # 現在編集中テンプレ名
    st.session_state.setdefault("tmpl_editor_name", "")
    st.session_state.setdefault("tmpl_editor_body", "")
    st.session_state.setdefault("current_template_id", None)  # 「現在のテンプレ」表示用
    # 選択状態の記録（自動更新用）
    st.session_state.setdefault("prev_selected_video_id", None)
    st.session_state.setdefault("prev_selected_template_id", None)

    creds: Optional[Credentials] = st.session_state["google_creds"]

    # ① Googleアカウント連携（メイン画面で実施）
    st.subheader("① Googleアカウント連携")

    if creds is None:
        st.write("まずは Google アカウントとの連携を行ってください。")
        st.caption("※Googleの認証画面が別タブで開きます。認証後、この画面に戻ってきてください。")
        start_google_oauth()
        st.stop()
    else:
        cols_auth = st.columns([3, 1])
        with cols_auth[0]:
            st.success("✅ Google認証済みです。")
        with cols_auth[1]:
            if st.button("認証をリセット"):
                for k in [
                    "google_creds",
                    "videos",
                    "tweet_text",
                    "templates_loaded",
                    "tmpl_editor_name",
                    "tmpl_editor_body",
                    "tmpl_picker",
                    "current_template_id",
                    "prev_selected_video_id",
                    "prev_selected_template_id",
                ]:
                    st.session_state.pop(k, None)
                st.rerun()

    # テンプレ読み込み（初回）
    if "templates_loaded" not in st.session_state:
        try:
            st.session_state["templates"] = load_templates_from_sheets(creds)
        except Exception as e:
            st.warning(f"テンプレ読み込みに失敗しました（初期テンプレを使用）：{e}")
        st.session_state["templates_loaded"] = True
    templates: List[Template] = st.session_state["templates"]

    # ② 対象動画とテンプレートの選択
    st.subheader("② 対象動画とテンプレートの選択")

    # 1行目：ボタンのみ
    if st.button("自分のチャンネルの予約動画リストを取得／更新"):
        try:
            st.session_state["videos"] = fetch_scheduled_videos(creds)
            if st.session_state["videos"]:
                st.success(f"{len(st.session_state['videos'])} 件の予約投稿／配信予定動画を取得しました。")
            else:
                st.warning("予約投稿中／配信予定の動画が見つかりませんでした。")
        except Exception as e:
            st.error(f"予約動画の取得に失敗しました：{e}")

    videos: List[Video] = st.session_state["videos"]

    if not videos:
        st.info("「自分のチャンネルの予約動画リストを取得／更新」ボタンで予約動画リストを取得してください。")
        return

    # 2行目：動画選択とテンプレ選択を横並び
    col_video_select, col_tmpl_select = st.columns([1, 1])

    with col_video_select:
        video_options = {
            f"{v.title} / {format_publish_at(v.publish_at_utc)}": v.video_id
            for v in videos
        }
        selected_label = st.selectbox(
            "予約動画を選んでください",
            list(video_options.keys()),
        )
        current_video = next(
            v for v in videos if v.video_id == video_options[selected_label]
        )

    with col_tmpl_select:
        tmpl_map = {t.name: t.id for t in templates}
        selected_tmpl_label = st.selectbox(
            "テンプレ（ツイートの型）を選んでください",
            list(tmpl_map.keys()),
        )
        selected_template = next(
            t for t in templates if t.id == tmpl_map[selected_tmpl_label]
        )

    # --- 選択変更時にツイート本文 & 現在テンプレを自動更新 ---
    prev_vid_id = st.session_state.get("prev_selected_video_id")
    prev_tmpl_id = st.session_state.get("prev_selected_template_id")

    is_first = (prev_vid_id is None and prev_tmpl_id is None)
    video_changed = (prev_vid_id is not None and prev_vid_id != current_video.video_id)
    tmpl_changed = (prev_tmpl_id is not None and prev_tmpl_id != selected_template.id)

    if is_first or video_changed or tmpl_changed:
        st.session_state["tmpl_picker"] = selected_template.name
        st.session_state["tmpl_editor_name"] = selected_template.name
        st.session_state["tmpl_editor_body"] = selected_template.body
        st.session_state["current_template_id"] = selected_template.id

        # 概要由来は最大200unitまで（改行保持）
        snippet = extract_snippet(current_video.description)
        tweet = build_tweet_from_template(
            selected_template.body,
            current_video,
            snippet,
        )
        st.session_state["tweet_text"] = tweet

    st.session_state["prev_selected_video_id"] = current_video.video_id
    st.session_state["prev_selected_template_id"] = selected_template.id

    if st.session_state["current_template_id"] is None:
        st.session_state["current_template_id"] = selected_template.id

    # ==============================
    # メイン：動画情報 & テンプレ編集
    # ==============================

    st.subheader("③ 　告知文を作成する")

    # タイトルと公開予定日時を横並び
    col_title, col_time = st.columns([3, 2])
    with col_title:
        st.write(f"**動画タイトル：** {current_video.title}")
    with col_time:
        st.write(f"**公開予定日時：** {format_publish_at_with_weekday(current_video.publish_at_utc)}")

    st.write(f"**動画URL：** {current_video.url}")

    # 概要欄（全文をプレビュー）
    with st.expander("概要欄を確認する"):
        st.text(current_video.description)

    # 現在のテンプレ
    cur_tmpl = next(
        (t for t in templates if t.id == st.session_state["current_template_id"]),
        selected_template,
    )
    st.markdown("#### 現在選択しているテンプレ")
    st.write(f"**テンプレ名：** {cur_tmpl.name}")
    st.code(cur_tmpl.body or "(本文なし)", language=None)

    # テンプレ編集
    with st.expander("テンプレを編集する"):
        if st.session_state["tmpl_picker"] is None:
            st.session_state["tmpl_picker"] = cur_tmpl.name
            st.session_state["tmpl_editor_name"] = cur_tmpl.name
            st.session_state["tmpl_editor_body"] = cur_tmpl.body

        picker_options = [t.name for t in templates]
        try:
            default_index = picker_options.index(st.session_state["tmpl_picker"])
        except ValueError:
            default_index = picker_options.index(cur_tmpl.name)

        picked = st.selectbox(
            "テンプレを選択",
            picker_options,
            index=default_index,
        )

        if picked != st.session_state["tmpl_picker"]:
            st.session_state["tmpl_picker"] = picked
            t = next(t for t in templates if t.name == picked)
            st.session_state["tmpl_editor_name"] = t.name
            st.session_state["tmpl_editor_body"] = t.body
            st.session_state["current_template_id"] = t.id

        ed_name = st.text_input("テンプレ名をつける", key="tmpl_editor_name")
        ed_body = st.text_area("テンプレ本文(編集用)", key="tmpl_editor_body", height=160)

        c1_btn, c2_btn, c3_btn = st.columns(3)
        with c1_btn:
            if st.button("💾 このテンプレを保存（上書き）"):
                try:
                    target = next(
                        t for t in templates
                        if t.name == st.session_state["tmpl_picker"]
                    )
                    target.name = ed_name
                    target.body = ed_body
                    save_templates_to_sheets(creds, templates)
                    st.session_state["tmpl_picker"] = ed_name
                    st.session_state["current_template_id"] = target.id
                    st.success("テンプレを上書き保存しました。")
                    st.rerun()
                except StopIteration:
                    st.error("対象テンプレが見つかりませんでした。")
                except Exception as e:
                    st.error(f"保存に失敗しました：{e}")

        with c2_btn:
            if st.button("➕ このテンプレを新規追加"):
                try:
                    new_id = next_template_id(templates)
                    new_tmpl = Template(
                        id=new_id,
                        name=(ed_name or f"新規テンプレ {new_id}").strip(),
                        body=ed_body,
                        is_default=False,
                    )
                    append_template_to_sheets(creds, new_tmpl)
                    st.session_state["templates"] = templates + [new_tmpl]
                    st.session_state["tmpl_picker"] = new_tmpl.name
                    st.session_state["current_template_id"] = new_tmpl.id
                    st.success(f"テンプレを新規追加しました（ID={new_id}）。")
                    st.rerun()
                except Exception as e:
                    st.error(f"新規追加に失敗しました：{e}")

        with c3_btn:
            if st.button("🗑 このテンプレを削除"):
                try:
                    remaining = [
                        t for t in templates
                        if t.name != st.session_state["tmpl_picker"]
                    ]
                    if len(remaining) == len(templates):
                        st.warning("削除対象のテンプレが見つかりません。")
                    elif not remaining:
                        st.warning("テンプレは最低1件必要なため、削除できません。")
                    else:
                        save_templates_to_sheets(creds, remaining)
                        st.session_state["templates"] = remaining
                        st.session_state["tmpl_picker"] = remaining[0].name
                        st.session_state["current_template_id"] = remaining[0].id
                        st.success("テンプレを削除しました。")
                        st.rerun()
                except Exception as e:
                    st.error(f"削除に失敗しました：{e}")

        st.markdown("---")
        if st.button("🌀 現在のテンプレを使用して再出力する↓"):
            # 最大200unit 上限（改行保持）
            snippet = extract_snippet(current_video.description)
            tweet = build_tweet_from_template(
                st.session_state["tmpl_editor_body"],
                current_video,
                snippet,
            )
            st.session_state["tweet_text"] = tweet
            st.success("現在のテンプレでツイート本文を再生成しました。")
            st.rerun()

        st.markdown("---")
        with st.expander("差し込みキーワードの意味", expanded=False):
            st.markdown("**タイトル**（動画タイトルがそのまま入ります）")
            st.code("{title}", language=None)

            st.markdown("**概要（冒頭200文字相当・改行保持）**")
            st.code("{snippet}", language=None)

            st.markdown("**動画URL**（YouTubeの動画URLが入ります。URLは24文字換算）")
            st.code("{url}", language=None)

            st.markdown("**予約済みの公開日時**（m月d日(曜) 形式で入ります。例：11月12日(水)）")
            st.code("{publish_at}", language=None)

    # ===== ツイート本文 =====

    # ここで再度、確認用にタイトルとURLを表示
    st.markdown("#### 対象動画（確認用）")
    col_conf_title, col_conf_time = st.columns([3, 2])
    with col_conf_title:
        st.write(f"**動画タイトル：** {current_video.title}")
    with col_conf_time:
        st.write(f"**公開予定日時：** {format_publish_at_with_weekday(current_video.publish_at_utc)}")
    st.write(f"**動画URL：** {current_video.url}")

    # 見出し
    st.markdown("#### ✏️ 投稿本文（ここで自由に編集できます）")

    # クリップボード用に、現時点の tweet_text をエスケープ（セッションから取得）
    current_text_for_copy = st.session_state.get("tweet_text", "") or ""
    safe_text = (
        current_text_for_copy
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("\n", "\\n")
    )

    # 「文字数カウント」「クリップボードにコピー」を同じスタイルで横並び表示
    buttons_html = f"""
    <div style="margin: 0.25rem 0 0.5rem 0; display: flex; flex-wrap: wrap; gap: 8px;">
      <button
        type="button"
        style="padding:8px 14px;border-radius:8px;border:1px solid #aaa;
               cursor:pointer;background-color:#f5f5f5;color:#333;
               font-size:14px;line-height:1.3;"
      >
        文字数カウント
      </button>
      <button
        type="button"
        style="padding:8px 14px;border-radius:8px;border:1px solid #aaa;
               cursor:pointer;background-color:#f5f5f5;color:#333;
               font-size:14px;line-height:1.3;"
        onclick='navigator.clipboard.writeText("{safe_text}")'
      >
        クリップボードにコピー
      </button>
    </div>
    """
    html(buttons_html, height=70)

    # テキストエリア本体
    tweet_text = st.text_area(
        label="",
        key="tweet_text",
        height=240,  # デフォルトの表示高さ（ここを変えれば行数感を調整可能）
    )

    # ヒント
    st.caption("本文を編集すると文字数カウントは自動で更新されます。必要に応じて「文字数カウント」ボタンを押して確認してください。")

    # 曜日付きのフォーマットで表示
    st.info(f"⏰ この動画の公開予定日時： {format_publish_at_with_weekday(current_video.publish_at_utc)}")

    # 文字数カウント（常に最新値を表示）
    body_units, url_units, url_count = count_units_breakdown(tweet_text or "")
    total_units = body_units + url_units
    if total_units > 280:
        st.error(
            f"現在 {total_units}字（本文{body_units}字 + URL{url_units}字 / URL本数 {url_count}）－ 280字を超えています。"
        )
    else:
        st.write(
            f"現在 **{total_units}字（本文{body_units}字 + URL{url_units}字）** ／ 280字"
        )

    # （コピー用ボタンは上で統一スタイルとして表示済み）


if __name__ == "__main__":
    main()
