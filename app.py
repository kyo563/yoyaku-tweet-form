import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

import streamlit as st
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest

# ==============================
# 設定
# ==============================

# YouTube + Google Sheets の最小権限
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

# テンプレ・下書き保管用スプレッドシートID（共用）
SPREADSHEET_ID = "1t34GoYFFHJdCsIjvbSGLEfD7W-cfeDgyAQoh9_u-oUU"

# Twitter上でのURLカウント（安全側に24文字として扱う）
URL_UNITS = 24

# ツイート本文のセッションキー
TWEET_BODY_KEY = "tweet_text"

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

def count_tweet_units_with_urls(text: str) -> int:
    url_pattern = re.compile(r"https?://\S+")
    total = 0
    pos = 0
    for m in url_pattern.finditer(text):
        before = text[pos:m.start()]
        total += count_units(before)
        total += URL_UNITS
        pos = m.end()
    total += count_units(text[pos:])
    return total

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
        if count_units(text) <= max_units:
            return text
        return truncate_to_limit(text, max_units=max_units)
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
# OAuth ユーティリティ
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
    return Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

def creds_from_dict(d: Dict[str, Any]) -> Credentials:
    """
    session_stateに置いた辞書からCredentialsを再構築。
    自動リフレッシュに必要な情報（client_id/secret, token_uri）を補完。
    """
    if not d:
        return None
    info = st.secrets["google_oauth"]
    return Credentials(
        token=d.get("token"),
        refresh_token=d.get("refresh_token"),
        token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=d.get("scopes", SCOPES),
        expiry=d.get("expiry"),
    )

def creds_to_dict(creds: Credentials) -> Dict[str, Any]:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "scopes": creds.scopes,
        "expiry": creds.expiry,
    }

def refresh_creds_if_needed(creds: Credentials) -> Credentials:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
    return creds

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
    # session_stateには辞書化して保存（オブジェクト直置きを避ける）
    st.session_state["google_creds_dict"] = creds_to_dict(creds)
    # クエリ掃除→再描画
    st.experimental_set_query_params()
    st.rerun()

def start_google_oauth(same_tab: bool = False):
    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        # 初回だけconsentでOK。毎回は不要だが、明示するならここを"consent"
        prompt="consent",
    )
    if same_tab:
        # 同タブに即遷移（新規タブを避ける）
        st.markdown(f'<meta http-equiv="refresh" content="0; url={auth_url}">', unsafe_allow_html=True)
    else:
        st.link_button("Googleアカウントで連携する", auth_url, use_container_width=True)

# ==============================
# YouTube API 呼び出し
# ==============================

def fetch_scheduled_videos(creds: Credentials) -> List[Video]:
    """
    本人チャンネルのアップロード動画の中から、
    「非公開」かつ「公開予定日時が未来」のものを予約動画として取得。
    """
    creds = refresh_creds_if_needed(creds)
    youtube = build("youtube", "v3", credentials=creds)
    now = datetime.now(timezone.utc)

    channels_resp = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = channels_resp.get("items", [])
    if not items:
        return []

    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # ページングで全videoId収集
    video_ids: List[str] = []
    page_token = None
    while True:
        playlist_items_resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        video_ids.extend([it["contentDetails"]["videoId"] for it in playlist_items_resp.get("items", [])])
        page_token = playlist_items_resp.get("nextPageToken")
        if not page_token:
            break

    if not video_ids:
        return []

    videos: List[Video] = []
    # 50件ずつバッチ
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
            # 予約はprivate+publishAt>now
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
# Google Sheets 連携（テンプレのみ利用）
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
    creds = refresh_creds_if_needed(creds)
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
    creds = refresh_creds_if_needed(creds)
    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()
    values = []
    for t in templates:
        values.append([
            t.id,
            t.name,
            t.body,
            "TRUE" if t.is_default else "FALSE",
        ])
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

# ==============================
# Streamlit アプリ本体
# ==============================

def main():
    st.set_page_config(page_title="予約投稿作成フォーム", layout="wide")
    st.title("📝 予約投稿作成フォーム（YouTube×X）")

    st.markdown("""
このフォームでは、**自分のYouTubeチャンネルの予約投稿動画**から  
「タイトル」「概要欄」「公開予定時間」を読み取り、  
**X（旧Twitter）の予約投稿用テキスト**を作成できます。

作成した文章はコピーして、X公式の予約投稿フォームに貼り付けてください。  
テンプレート文章は、共通のGoogleスプレッドシートで管理・編集します。
""")

    # ===== OAuth コールバック処理 =====
    handle_oauth_callback()

    # ----- セッション初期化 -----
    st.session_state.setdefault("google_creds_dict", None)
    st.session_state.setdefault("videos", [])
    st.session_state.setdefault("templates", default_templates())
    st.session_state.setdefault("current_tweet", "")
    st.session_state.setdefault(TWEET_BODY_KEY, "")

    # Credentials再構築 & リフレッシュ
    creds_dict = st.session_state.get("google_creds_dict")
    creds: Optional[Credentials] = creds_from_dict(creds_dict) if creds_dict else None
    if creds:
        creds = refresh_creds_if_needed(creds)
        st.session_state["google_creds_dict"] = creds_to_dict(creds)

    # ----------------------
    # サイドバー：認証
    # ----------------------
    st.sidebar.header("① Googleアカウント連携")

    same_tab_auth = st.sidebar.checkbox("認証を同タブで開く（新規タブを避ける）", value=True)

    if not creds:
        st.sidebar.write("まずは Google アカウントで認証を行います。")
        if st.sidebar.button("Google連携を開始する", use_container_width=True):
            start_google_oauth(same_tab=same_tab_auth)
            st.stop()
    else:
        st.sidebar.success("✅ Google認証済み")
        if st.sidebar.button("認証をリセットする（このブラウザだけ）", use_container_width=True):
            st.session_state["google_creds_dict"] = None
            st.session_state["videos"] = []
            st.session_state["current_tweet"] = ""
            st.session_state[TWEET_BODY_KEY] = ""
            st.rerun()

    if not creds:
        st.info("左のサイドバーから Google アカウント連携を行ってください。")
        return

    # 認証済みなら、テンプレをスプレッドシートから読み込む（初回のみ）
    if "templates_loaded" not in st.session_state:
        try:
            st.session_state["templates"] = load_templates_from_sheets(creds)
            st.session_state["templates_loaded"] = True
        except Exception as e:
            st.warning(f"テンプレート読み込みに失敗しました（初期テンプレを使用）：{e}")
    templates: List[Template] = st.session_state["templates"]

    # ----------------------
    # サイドバー：予約動画取得
    # ----------------------
    st.sidebar.header("② YouTube予約動画の取得")
    if st.sidebar.button("自分のチャンネルの予約動画リストを取得／更新", use_container_width=True):
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
    # サイドバー：動画選択 & テンプレ選択
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
    # メイン：動画情報 & ツイート編集
    # ----------------------
    st.subheader("📝 自動生成と編集")
    st.write(f"**動画タイトル：** {current_video.title}")
    st.write(f"**公開予定日時：** {format_publish_at(current_video.publish_at_utc)}")
    st.write(f"**動画URL：** {current_video.url}")

    with st.expander("概要欄を確認する"):
        st.text(current_video.description)

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("📄 テンプレート本文を呼び出す", use_container_width=True):
            base = selected_template.body or ""
            st.session_state["current_tweet"] = base
            st.session_state[TWEET_BODY_KEY] = base
            st.success("選択中のテンプレート本文を、下のツイート欄に呼び出しました。")

    with col_btn2:
        if st.button("🔧 概要欄からツイート文を自動作成", use_container_width=True):
            snippet = extract_snippet(current_video.description)
            tweet = build_tweet_from_template(selected_template.body, current_video, snippet)
            st.session_state["current_tweet"] = tweet
            st.session_state[TWEET_BODY_KEY] = tweet
            st.success("概要欄とテンプレートを使って、ツイート文を自動作成しました。")

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
        st.session_state.setdefault(tmpl_body_key, selected_template.body or "")

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

        def safe_insert_placeholder(ph: str, where: str = "append"):
            cur = st.session_state.get(tmpl_body_key, "")
            cur = "" if cur is None else str(cur)
            ph = "" if ph is None else str(ph)

            if where == "prepend":
                new_val = ph + ("" if not cur else "\n" + cur)
            elif where == "replace":
                new_val = ph
            else:
                new_val = cur + ("" if not cur else "\n") + ph

            st.session_state.setdefault(tmpl_body_key, "")
            st.session_state[tmpl_body_key] = new_val
            st.rerun()

        col_ins1, col_ins2, col_ins3, col_ins4 = st.columns(4)
        with col_ins1:
            if st.button("タイトル", key=f"ins_title_{selected_template.id}"):
                safe_insert_placeholder("{title}")
        with col_ins2:
            if st.button("概要（自動要約）", key=f"ins_snippet_{selected_template.id}"):
                safe_insert_placeholder("{snippet}")
        with col_ins3:
            if st.button("動画URL", key=f"ins_url_{selected_template.id}"):
                safe_insert_placeholder("{url}")
        with col_ins4:
            if st.button("公開日時", key=f"ins_publish_{selected_template.id}"):
                safe_insert_placeholder("{publish_at}")

        st.markdown("###### 差し込みキーワードの意味")
        st.markdown("**タイトル**（動画タイトルがそのまま入ります）")
        st.code("{title}", language=None)

        st.markdown("**概要（自動要約）**（概要欄から自動で抜き出した短い説明文が入ります）")
        st.code("{snippet}", language=None)

        st.markdown("**動画URL**（YouTubeの動画URLが入ります）")
        st.code("{url}", language=None)

        st.markdown("**公開日時**（動画の公開予定日時が入ります。例：2025/01/23 20:00）")
        st.code("{publish_at}", language=None)

        with col_save:
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

    # ----------------------
    # ツイート本文編集（リアルタイム文字数カウント）
    # ----------------------
    tweet_text = st.text_area(
        "✏️ ツイート本文（ここで自由に編集できます。改行もそのまま反映されます）",
        key=TWEET_BODY_KEY,
        height=200,
    )
    st.session_state["current_tweet"] = tweet_text

    units = count_tweet_units_with_urls(tweet_text or "")
    if units > 280:
        st.error(f"現在 {units} / 280 文字相当です。（少し削ってください）")
    else:
        st.write(f"現在 {units} / 280 文字相当です。")

    # ----------------------
    # コピー用プレビュー
    # ----------------------
    st.markdown("### 📋 ツイート最終確認＆コピー")
    st.info(f"⏰ **この動画の公開予定日時： {format_publish_at(current_video.publish_at_utc)}**")
    st.caption("※下の枠の右上にあるコピーアイコンを押すと、ツイート文をクリップボードにコピーできます。")

    if tweet_text:
        st.code(tweet_text, language=None)
    else:
        st.info("「テンプレート本文を呼び出す」か「概要欄から自動作成」を押して、ツイート文を作成してください。")

    st.markdown("---")
    st.caption("💡 公開予定の日時を見ながら、X公式の予約投稿フォームで同じ時間に投稿予約を設定してください。")

if __name__ == "__main__":
    main()
