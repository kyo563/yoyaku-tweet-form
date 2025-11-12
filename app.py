import re
import unicodedata
import uuid
import json
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

# 共有スプレッドシートID
SPREADSHEET_ID = "1t34GoYFFHJdCsIjvbSGLEfD7W-cfeDgyAQoh9_u-oUU"

# X(Twitter)上のURL固定長（安全側24）
URL_UNITS = 24


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


@dataclass
class TweetDraft:
    video_id: str
    tweet_text: str
    publish_at_utc: datetime
    title: str


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
    result_chars = []
    length = 0
    for ch in text:
        w = unicodedata.east_asian_width(ch)
        add = 2 if w in ("F", "W", "A") else 1
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
    本文(非URL)の長さ・URL部の合計長(24×本数)・URL本数 を返す。
    """
    url_pattern = re.compile(r"https?://\S+")
    body_units = 0
    url_count = 0
    pos = 0
    for m in url_pattern.finditer(text):
        before = text[pos:m.start()]
        body_units += count_units(before)
        url_count += 1
        pos = m.end()
    body_units += count_units(text[pos:])
    url_units = URL_UNITS * url_count
    return body_units, url_units, url_count


def count_tweet_units_with_urls(text: str) -> int:
    body_units, url_units, _ = count_units_breakdown(text)
    return body_units + url_units


def extract_snippet(description: str, max_units: int = 200) -> str:
    lines = description.splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if re.search(r"https?://", line):
            continue
        cleaned.append(line)
    text = " ".join(cleaned)
    return truncate_to_limit(text, max_units=max_units)


def format_publish_at(dt: datetime, tz_name: str = "Asia/Tokyo") -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        local = dt.astimezone(tz)
    except Exception:
        local = dt
    return local.strftime("%Y/%m/%d %H:%M")


def ensure_url_and_limit(text: str, url: str, max_units: int = 280) -> str:
    if not url or url not in text:
        return text if count_units(text) <= max_units else truncate_to_limit(text, max_units=max_units)

    idx = text.rfind(url)
    prefix = text[:idx]

    prefix_units = count_units(prefix)
    total_units = prefix_units + URL_UNITS

    if total_units <= max_units:
        return prefix + url

    allowed_prefix_units = max_units - URL_UNITS
    if allowed_prefix_units <= 0:
        return url

    truncated_prefix = truncate_to_limit(prefix, max_units=allowed_prefix_units)
    return truncated_prefix + url


def build_tweet_from_template(template_body: str, video: Video, snippet: str, max_units: int = 280) -> str:
    publish_at_str = format_publish_at(video.publish_at_utc)
    url = video.url
    raw = template_body.format(
        title=video.title,
        url=url,
        snippet=snippet,
        publish_at=publish_at_str,
    )
    tweet = ensure_url_and_limit(raw, url, max_units=max_units)
    return tweet


# ==============================
# Google OAuth（Webアプリ用）
# ==============================

def get_client_config() -> dict:
    info = st.secrets["google_oauth"]
    client_config = {
        "web": {
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "auth_uri": info.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": info.get("token_uri", "https://oauth2.googleapis.com/token"),
            "redirect_uris": [info["redirect_uri"]],
        }
    }
    return client_config


def create_flow() -> Flow:
    client_config = get_client_config()
    redirect_uri = st.secrets["google_oauth"]["redirect_uri"]
    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def handle_oauth_callback():
    params = st.experimental_get_query_params()
    if "code" not in params:
        return
    code = params.get("code", [None])[0]
    if code is None:
        return
    flow = create_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    st.session_state["google_creds"] = creds
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
    creds = ensure_valid_creds(creds)
    youtube = build("youtube", "v3", credentials=creds)
    now = datetime.now(timezone.utc)

    channels_resp = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = channels_resp.get("items", [])
    if not items:
        return []

    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids: List[str] = []
    page_token = None
    while True:
        playlist_items_resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        video_ids += [it["contentDetails"]["videoId"] for it in playlist_items_resp.get("items", [])]
        page_token = playlist_items_resp.get("nextPageToken")
        if not page_token:
            break

    if not video_ids:
        return []

    videos: List[Video] = []
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i: i + 50]
        resp = youtube.videos().list(part="snippet,status", id=",".join(batch_ids)).execute()
        for item in resp.get("items", []):
            status = item.get("status", {})
            snippet = item.get("snippet", {})
            publish_at_str = status.get("publishAt")
            if not publish_at_str:
                continue
            try:
                publish_dt = datetime.fromisoformat(publish_at_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if status.get("privacyStatus") != "private":
                continue
            if publish_dt <= now:
                continue
            videos.append(
                Video(
                    video_id=item["id"],
                    title=snippet.get("title", ""),
                    description=snippet.get("description", ""),
                    publish_at_utc=publish_dt,
                )
            )
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
            is_default=True,
        ),
        Template(
            id="2",
            name="丁寧めなお知らせ",
            body="本日 {publish_at} に動画を公開予定です。\n\n{snippet}\n\n{url}",
            is_default=False,
        ),
    ]


def load_templates_from_sheets(creds: Credentials) -> List[Template]:
    if not SPREADSHEET_ID:
        return default_templates()
    creds = ensure_valid_creds(creds)
    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Templates!A2:D",
    ).execute()
    values = result.get("values", [])
    if not values:
        return default_templates()

    templates: List[Template] = []
    for row in values:
        t_id = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else ""
        body = row[2] if len(row) > 2 else ""
        is_default_str = row[3] if len(row) > 3 else "FALSE"
        is_default = str(is_default_str).upper() == "TRUE"
        if not t_id or not name or not body:
            continue
        templates.append(Template(id=t_id, name=name, body=body, is_default=is_default))
    return templates or default_templates()


def save_templates_to_sheets(creds: Credentials, templates: List[Template]) -> None:
    if not SPREADSHEET_ID:
        return
    creds = ensure_valid_creds(creds)
    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()
    values = [[t.id, t.name, t.body, "TRUE" if t.is_default else "FALSE"] for t in templates]
    sheet.values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range="Templates!A2:D",
    ).execute()
    if not values:
        return
    sheet.values().update(
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
    sheet = service.spreadsheets()
    values = [[template.id, template.name, template.body, "TRUE" if template.is_default else "FALSE"]]
    sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Templates!A:D",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def next_template_id(existing: List[Template]) -> str:
    nums = []
    for t in existing:
        try:
            nums.append(int(t.id))
        except Exception:
            pass
    if nums:
        return str(max(nums) + 1)
    return uuid.uuid4().hex[:8]


# ==============================
# Streamlit アプリ本体
# ==============================

def main():
    st.set_page_config(page_title="予約投稿作成フォーム", layout="wide")
    st.title("📝 予約投稿作成フォーム（YouTube×X）")

    # 目立つCSS（テンプレ編集expanderの強調）
    st.markdown(
        """
        <style>
        /* すべてのExpanderを軽く強調（特にテンプレ編集を目立たせる） */
        div[data-testid="stExpander"] > details {
            border: 1px solid #f0c36d22;
            border-radius: 10px;
            background: #fff8e1aa; /* 薄い黄色 */
        }
        div[data-testid="stExpander"] summary {
            background: #ffe8a1;
            border-radius: 10px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
このフォームでは、**自分のYouTubeチャンネルの予約投稿動画**から  
「タイトル」「概要欄」「公開予定時間」を読み取り、  
**X（旧Twitter）の予約投稿用テキスト**を作成できます。
        """
    )

    # OAuth コールバック処理
    handle_oauth_callback()

    if "google_creds" not in st.session_state:
        st.session_state["google_creds"] = None
    if "videos" not in st.session_state:
        st.session_state["videos"] = []
    if "templates" not in st.session_state:
        st.session_state["templates"] = default_templates()
    if "current_tweet" not in st.session_state:
        st.session_state["current_tweet"] = ""
    if "tweet_text" not in st.session_state:
        st.session_state["tweet_text"] = ""

    creds: Optional[Credentials] = st.session_state["google_creds"]
    templates: List[Template] = st.session_state["templates"]

    # ----------------------
    # サイドバー：認証
    # ----------------------
    st.sidebar.header("① Googleアカウント連携")
    if creds is None:
        st.sidebar.write("まずは Google アカウントで認証を行います。")
        st.sidebar.caption("※Google公式の安全な認証画面が別タブで開きます。")
        if st.sidebar.button("Google連携を開始する"):
            start_google_oauth()
            st.stop()
    else:
        st.sidebar.success("✅ Google認証済み")
        if st.sidebar.button("認証をリセットする（このブラウザだけ）"):
            st.session_state["google_creds"] = None
            st.session_state["videos"] = []
            st.session_state["current_tweet"] = ""
            st.session_state["tweet_text"] = ""
            st.rerun()

    if creds is None:
        st.info("左のサイドバーから Google アカウント連携を行ってください。")
        return

    # 認証済みなら、テンプレ読み込み（初回のみ）
    if "templates_loaded" not in st.session_state:
        try:
            st.session_state["templates"] = load_templates_from_sheets(creds)
            st.session_state["templates_loaded"] = True
        except Exception as e:
            st.warning(f"テンプレート読み込みに失敗しました（初期テンプレを使用）：{e}")
    templates = st.session_state["templates"]

    # ----------------------
    # サイドバー：予約動画取得
    # ----------------------
    st.sidebar.header("② YouTube予約動画の取得")
    if st.sidebar.button("自分のチャンネルの予約動画リストを取得／更新"):
        try:
            videos = fetch_scheduled_videos(creds)
            st.session_state["videos"] = videos
            if videos:
                st.sidebar.success(f"{len(videos)} 件の予約動画を取得しました。")
            else:
                st.sidebar.warning("予約投稿中の動画が見つかりませんでした。")
        except Exception as e:
            st.sidebar.error(f"予約動画の取得に失敗しました：{e}")

    videos: List[Video] = st.session_state["videos"]
    if not videos:
        st.info("左のサイドバーで「予約動画リストを取得／更新」を実行してください。")
        return

    # ----------------------
    # サイドバー：動画 & テンプレ選択
    # ----------------------
    st.sidebar.header("③ 対象動画とテンプレの選択")
    video_options = {f"{v.title} / {format_publish_at(v.publish_at_utc)}": v.video_id for v in videos}
    selected_label = st.sidebar.selectbox("予約動画を選んでください", list(video_options.keys()))
    selected_video_id = video_options[selected_label]
    current_video = next(v for v in videos if v.video_id == selected_video_id)

    template_names = {t.name: t.id for t in templates}
    selected_template_label = st.sidebar.selectbox(
        "テンプレート（ツイートの型）を選んでください",
        list(template_names.keys()),
    )
    selected_template = next(t for t in templates if t.id == template_names[selected_template_label])

    # ----------------------
    # メイン：動画情報 & テンプレ呼び出し
    # ----------------------
    st.subheader("📝 自動生成と編集")
    st.write(f"**動画タイトル：** {current_video.title}")
    st.write(f"**公開予定日時：** {format_publish_at(current_video.publish_at_utc)}")
    st.write(f"**動画URL：** {current_video.url}")

    with st.expander("概要欄を確認する"):
        st.text(current_video.description)

    # テンプレ呼び出し（統合：押したら差し込みまで自動）
    with st.popover("📄 テンプレートを呼び出す（一覧から選択）", use_container_width=True):
        name_map = {(f"★ {t.name}" if t.is_default else t.name): t.id for t in templates}
        labels_sorted = sorted(name_map.keys(), key=lambda x: (not x.startswith("★ "), x.lower()))
        sel_label = st.radio("テンプレートを選んでください", options=labels_sorted, index=0)
        sel_tmpl = next(t for t in templates if t.id == name_map[sel_label])

        st.text_area("プレビュー", value=sel_tmpl.body, height=140, disabled=True)

        if st.button("このテンプレを本文に反映する（自動差し込み）", use_container_width=True, key=f"apply_auto_{sel_tmpl.id}"):
            snippet = extract_snippet(current_video.description)
            tweet = build_tweet_from_template(sel_tmpl.body, current_video, snippet)
            st.session_state["current_tweet"] = tweet
            st.session_state["tweet_text"] = tweet
            st.success("テンプレ＋差し込みで本文を作成・反映しました。")
            st.rerun()

    # ----------------------
    # ツイート本文（ここに最終確認＆コピーを統合）
    # ----------------------
    if "tweet_text" not in st.session_state:
        st.session_state["tweet_text"] = st.session_state["current_tweet"]

    tweet_text = st.text_area(
        "✏️ ツイート本文（ここで自由に編集できます。改行もそのまま反映されます）",
        key="tweet_text",
        height=240,
    )
    st.session_state["current_tweet"] = tweet_text

    # 公開予定日時（本文欄の直下に表示）
    st.info(f"⏰ この動画の公開予定日時： {format_publish_at(current_video.publish_at_utc)}")

    # 文字数（本文/URL内訳）表示
    body_units, url_units, url_count = count_units_breakdown(tweet_text)
    total_units = body_units + url_units
    if total_units > 280:
        st.error(f"現在 {total_units}字（本文{body_units}字 + URL{url_units}字 / URL本数 {url_count}） － 280字を超えています。")
    else:
        st.write(f"現在 **{total_units}字（本文{body_units}字 + URL{url_units}字）** ／ 280字")

    # コピー（本文欄の直下に設置）
    html(
        f"""
        <div style="margin: 0.5rem 0 1rem 0;">
          <button
            style="padding:8px 14px;border-radius:8px;border:1px solid #aaa;cursor:pointer;"
            onclick='navigator.clipboard.writeText({json.dumps(tweet_text)})'>
            クリップボードにコピー
          </button>
          <span style="margin-left:8px;color:#666;font-size:0.9rem;">本文全体をコピーします。</span>
        </div>
        """,
        height=60,
    )

    # ----------------------
    # テンプレート編集（差し込みキーワード＋保存・削除）
    # ----------------------
    with st.expander("🔧 テンプレート編集（今選んでいるテンプレを直接編集できます）"):
        tmpl_name = st.text_input(
            "テンプレート名",
            value=selected_template.name,
            key=f"tmpl_name_{selected_template.id}",
            help="テンプレ一覧で表示される名前です。",
        )

        tmpl_body_key = f"tmpl_body_{selected_template.id}"
        if tmpl_body_key not in st.session_state:
            st.session_state[tmpl_body_key] = selected_template.body or ""

        tmpl_body = st.text_area(
            "テンプレート本文",
            key=tmpl_body_key,
            height=150,
            help="下の「差し込みキーワード」ボタンを使うと、タイトルやURLなどを自動で入れられます。",
        )

        col_def, col_save, col_del = st.columns(3)
        with col_def:
            tmpl_default = st.checkbox(
                "このテンプレートをデフォルトにする",
                value=selected_template.is_default,
                key=f"tmpl_default_{selected_template.id}",
            )

        st.markdown("##### 差し込みキーワードを挿入する")
        st.caption("ボタンを押すと、テンプレート本文の末尾にキーワードが追加されます。")

        def append_placeholder(ph: str):
            cur = st.session_state.get(tmpl_body_key, "")
            cur = "" if cur is None else str(cur)
            ph = "" if ph is None else str(ph)
            st.session_state.setdefault(tmpl_body_key, "")
            st.session_state[tmpl_body_key] = cur + ph
            st.rerun()

        col_ins1, col_ins2, col_ins3, col_ins4 = st.columns(4)
        with col_ins1:
            if st.button("タイトル", key=f"ins_title_{selected_template.id}"):
                append_placeholder("{title}")
        with col_ins2:
            if st.button("概要（自動要約）", key=f"ins_snippet_{selected_template.id}"):
                append_placeholder("{snippet}")
        with col_ins3:
            if st.button("動画URL", key=f"ins_url_{selected_template.id}"):
                append_placeholder("{url}")
        with col_ins4:
            if st.button("公開日時", key=f"ins_publish_{selected_template.id}"):
                append_placeholder("{publish_at}")

        st.markdown("###### 差し込みキーワードの意味")
        st.code("{title}", language=None)
        st.code("{snippet}", language=None)
        st.code("{url}", language=None)
        st.code("{publish_at}", language=None)

        with col_save:
            # 上書き保存
            if st.button("💾 このテンプレートを保存", key=f"save_tmpl_{selected_template.id}"):
                body_to_save = st.session_state.get(tmpl_body_key, selected_template.body or "")
                for t in templates:
                    if t.id == selected_template.id:
                        t.name = tmpl_name
                        t.body = body_to_save
                        t.is_default = tmpl_default
                if tmpl_default:
                    for t in templates:
                        if t.id != selected_template.id:
                            t.is_default = False
                st.session_state["templates"] = templates
                try:
                    save_templates_to_sheets(creds, templates)
                    st.success("テンプレートをスプレッドシートに保存しました。")
                except Exception as e:
                    st.error(f"テンプレートの保存に失敗しました：{e}")

            # 追記保存
            if st.button("➕ 新規として追加保存（追記）", key=f"append_tmpl_{selected_template.id}"):
                body_to_save = st.session_state.get(tmpl_body_key, selected_template.body or "")
                new_id = next_template_id(templates)
                new_tmpl = Template(
                    id=new_id,
                    name=tmpl_name,
                    body=body_to_save,
                    is_default=tmpl_default,
                )
                try:
                    append_template_to_sheets(creds, new_tmpl)
                    st.session_state["templates"] = templates + [new_tmpl]
                    st.success(f"テンプレートを新規行（ID={new_id}）として追記しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"追記保存に失敗しました：{e}")

        with col_del:
            if st.button("🗑 このテンプレートを削除", key=f"del_tmpl_{selected_template.id}"):
                if len(templates) <= 1:
                    st.warning("テンプレートは最低1件必要なため、削除できません。")
                else:
                    new_templates = [t for t in templates if t.id != selected_template.id]
                    st.session_state["templates"] = new_templates
                    try:
                        save_templates_to_sheets(creds, new_templates)
                        st.success("テンプレートを削除しました。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"テンプレートの削除に失敗しました：{e}")


if __name__ == "__main__":
    main()
