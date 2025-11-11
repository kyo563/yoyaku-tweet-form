import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import streamlit as st
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# ==============================
# 設定
# ==============================

# YouTube + Google Sheets 両方にアクセスするスコープ
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

# テンプレ・下書き保管用スプレッドシートID（共用）
# https://docs.google.com/spreadsheets/d/1t34GoYFFHJdCsIjvbSGLEfD7W-cfeDgyAQoh9_u-oUU/edit
SPREADSHEET_ID = "1t34GoYFFHJdCsIjvbSGLEfD7W-cfeDgyAQoh9_u-oUU"


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
    """全角=2, 半角=1 で文字数をカウントします。"""
    total = 0
    for ch in text:
        w = unicodedata.east_asian_width(ch)
        total += 2 if w in ("F", "W", "A") else 1
    return total


def truncate_to_limit(text: str, max_units: int = 280) -> str:
    """max_units を超えないよう末尾をカットし、超えた場合は…を付けます。"""
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


def extract_snippet(description: str, max_units: int = 200) -> str:
    """概要欄からツイート用の抜粋を生成します。"""
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
    """表示用の日時文字列に変換します。"""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        local = dt.astimezone(tz)
    except Exception:
        local = dt
    return local.strftime("%Y/%m/%d %H:%M")


def build_tweet_from_template(template_body: str, video: Video, snippet: str, max_units: int = 280) -> str:
    """テンプレートに各種情報を埋め込み、文字数制限内で整えます。"""
    publish_at_str = format_publish_at(video.publish_at_utc)
    raw = template_body.format(
        title=video.title,
        url=video.url,
        snippet=snippet,
        publish_at=publish_at_str,
    )
    return truncate_to_limit(raw, max_units=max_units)


# ==============================
# Google OAuth（Webアプリ用）
# ==============================

def get_client_config() -> dict:
    """
    Streamlit Secrets から Webアプリ用OAuthクライアント設定を取得します。

    Streamlit Cloud の Secrets には、次のような形で設定しておきます:

    [google_oauth]
    client_id = "xxxxxxxxxxxxxxxx.apps.googleusercontent.com"
    client_secret = "xxxxxxxxxxxxxxx"
    auth_uri = "https://accounts.google.com/o/oauth2/auth"
    token_uri = "https://oauth2.googleapis.com/token"
    redirect_uri = "https://yoyaku-tweet-form-xxxxx.streamlit.app/"
    """
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
    """
    Google OAuth 用の Flow オブジェクトを作成します。
    state は使わず、redirect_uri だけ指定するシンプル版です。
    """
    client_config = get_client_config()
    redirect_uri = st.secrets["google_oauth"]["redirect_uri"]
    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def handle_oauth_callback():
    """
    Google からのリダイレクト（?code=...）が来たときにトークンを取得し、
    st.session_state["google_creds"] に保存します。
    """
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

    # URLのクエリパラメータをクリアしてリロード
    st.experimental_set_query_params()
    st.rerun()



def start_google_oauth():
    """
    Google 認証を開始し、認証URLへのリンクを表示します。
    """
    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    st.markdown(f"[Googleアカウントで連携する]({auth_url})")


# ==============================
# YouTube API 呼び出し
# ==============================

def fetch_scheduled_videos(creds: Credentials) -> List[Video]:
    """
    本人チャンネルのアップロード動画の中から、
    「非公開」かつ「公開予定日時が未来」のものを予約動画として取得します。
    """
    youtube = build("youtube", "v3", credentials=creds)
    now = datetime.now(timezone.utc)

    # 自分のチャンネルの uploads プレイリストIDを取得
    channels_resp = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = channels_resp.get("items", [])
    if not items:
        return []

    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # uploads プレイリスト内の動画IDを取得
    playlist_items_resp = youtube.playlistItems().list(
        part="contentDetails", playlistId=uploads_playlist_id, maxResults=50
    ).execute()
    video_ids = [item["contentDetails"]["videoId"] for item in playlist_items_resp.get("items", [])]
    if not video_ids:
        return []

    # videos.list で詳細情報取得
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
# Google Sheets 連携（テンプレ & 下書き）
# ==============================

def default_templates() -> List[Template]:
    """スプレッドシートが空のときに使うアプリ内デフォルトテンプレです。"""
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
    """
    スプレッドシートの Templates シートからテンプレを読み込みます。
    1行目はヘッダー行という前提で、A2:D 以降を読み込みます。
    何も登録されていなければ、アプリ内のデフォルトテンプレを返します。
    """
    if not SPREADSHEET_ID:
        return default_templates()

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

    if not templates:
        return default_templates()
    return templates


def save_templates_to_sheets(creds: Credentials, templates: List[Template]) -> None:
    """
    Templates シートに現在のテンプレ一覧をまるごと保存します。
    A2:D を一度クリアしてから書き換えます。
    """
    if not SPREADSHEET_ID:
        return

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


def load_tweet_draft_from_sheets(creds: Credentials, video_id: str) -> Optional[TweetDraft]:
    """
    TweetDrafts シートから、指定video_idの下書きを1件読み込みます。
    """
    if not SPREADSHEET_ID:
        return None

    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()

    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="TweetDrafts!A2:E",
    ).execute()
    values = result.get("values", [])

    for row in values:
        if not row:
            continue
        vid = row[0]
        if vid != video_id:
            continue
        title = row[1] if len(row) > 1 else ""
        publish_at_str = row[2] if len(row) > 2 else ""
        tweet_text = row[3] if len(row) > 3 else ""
        try:
            publish_dt = datetime.fromisoformat(publish_at_str)
        except Exception:
            publish_dt = datetime.now(timezone.utc)
        return TweetDraft(
            video_id=vid,
            tweet_text=tweet_text,
            publish_at_utc=publish_dt,
            title=title,
        )
    return None


def save_tweet_draft_to_sheets(creds: Credentials, draft: TweetDraft) -> None:
    """
    TweetDrafts シートに下書きを保存します。
    すでに同じ video_id の行があれば上書きし、なければ追加します。
    """
    if not SPREADSHEET_ID:
        return

    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()

    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="TweetDrafts!A2:E",
    ).execute()
    values = result.get("values", [])

    rows = values if values else []
    target_row_index = None  # 0ベース（A2が index=0）

    for idx, row in enumerate(rows):
        if row and row[0] == draft.video_id:
            target_row_index = idx
            break

    now_iso = datetime.now(timezone.utc).isoformat()

    new_row = [
        draft.video_id,
        draft.title,
        draft.publish_at_utc.isoformat(),
        draft.tweet_text,
        now_iso,
    ]

    if target_row_index is None:
        sheet.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="TweetDrafts!A2:E",
            valueInputOption="RAW",
            body={"values": [new_row]},
        ).execute()
    else:
        row_num = target_row_index + 2
        sheet.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"TweetDrafts!A{row_num}:E{row_num}",
            valueInputOption="RAW",
            body={"values": [new_row]},
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
**X（旧Twitter）の予約投稿用テキスト**を作成・保存できます。

作成した文章はコピーして、X公式の予約投稿フォームに貼り付けてください。  
テンプレート文章と各動画の下書きは、共通のGoogleスプレッドシートに保存されます。
""")

    # OAuth コールバック処理（?code=... が付いているとき）
    handle_oauth_callback()

    if "google_creds" not in st.session_state:
        st.session_state["google_creds"] = None
    if "videos" not in st.session_state:
        st.session_state["videos"] = []
    if "templates" not in st.session_state:
        st.session_state["templates"] = default_templates()
    if "current_tweet" not in st.session_state:
        st.session_state["current_tweet"] = ""

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
            st.experimental_rerun()

    # 認証前ならここで終了
    if creds is None:
        st.info("左のサイドバーから Google アカウント連携を行ってください。")
        return

    # 認証済みなら、テンプレをスプレッドシートから読み込む（初回のみ）
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
    # サイドバー：動画選択 & テンプレ選択
    # ----------------------
    st.sidebar.header("③ 対象動画とテンプレの選択")

    video_options = {f"{v.title} / {format_publish_at(v.publish_at_utc)}": v.video_id for v in videos}
    selected_label = st.sidebar.selectbox("予約動画を選んでください", list(video_options.keys()))
    selected_video_id = video_options[selected_label]
    current_video = next(v for v in videos if v.video_id == selected_video_id)

    template_names = {t.name: t.id for t in templates}
    selected_template_label = st.sidebar.selectbox(
        "ツイートの型（テンプレ）を選んでください",
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

    col_btn1, col_btn2, col_btn3 = st.columns(3)
    with col_btn1:
        if st.button("🔧 概要欄からツイート文を自動作成"):
            snippet = extract_snippet(current_video.description)
            tweet = build_tweet_from_template(selected_template.body, current_video, snippet)
            st.session_state["current_tweet"] = tweet
            st.success("ツイート文を自動で作成しました。内容を下で確認・修正できます。")
    with col_btn2:
        if st.button("💾 この動画の下書きをスプレッドシートから読み込み"):
            try:
                draft = load_tweet_draft_from_sheets(creds, current_video.video_id)
                if draft:
                    st.session_state["current_tweet"] = draft.tweet_text
                    st.success("保存済みの下書きを読み込みました。")
                else:
                    st.info("この動画の保存済み下書きはありません。")
            except Exception as e:
                st.error(f"下書きの読み込みに失敗しました：{e}")
    with col_btn3:
        pass

    # ----------------------
    # テンプレート編集
    # ----------------------
    with st.expander("🔧 テンプレート編集（今選んでいるテンプレを直接編集できます）"):
        tmpl_name = st.text_input(
            "テンプレート名",
            value=selected_template.name,
            key=f"tmpl_name_{selected_template.id}",
            help="テンプレ一覧で表示される名前です。",
        )
        tmpl_body = st.text_area(
            "テンプレート本文",
            value=selected_template.body,
            key=f"tmpl_body_{selected_template.id}",
            height=150,
            help="{title} / {snippet} / {url} / {publish_at} が使えます。",
        )
        tmpl_default = st.checkbox(
            "このテンプレートをデフォルトにする",
            value=selected_template.is_default,
            key=f"tmpl_default_{selected_template.id}",
        )

        if st.button("このテンプレートを保存する（スプレッドシート更新）"):
            for t in templates:
                if t.id == selected_template.id:
                    t.name = tmpl_name
                    t.body = tmpl_body
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
                st.error(f("テンプレートの保存に失敗しました：{e}"))

    # ----------------------
    # ツイート本文編集
    # ----------------------
    tweet_text = st.text_area(
        "✏️ ツイート本文（ここで自由に編集できます。改行もそのまま反映されます）",
        value=st.session_state["current_tweet"],
        height=200,
    )
    st.session_state["current_tweet"] = tweet_text

    units = count_units(tweet_text)
    if units > 280:
        st.error(f"現在 {units} / 280 文字相当です。（少し削ってください）")
    else:
        st.write(f"現在 {units} / 280 文字相当です。")

    if st.button("💾 このツイート文を下書きとしてスプレッドシートに保存"):
        try:
            draft = TweetDraft(
                video_id=current_video.video_id,
                tweet_text=tweet_text,
                publish_at_utc=current_video.publish_at_utc,
                title=current_video.title,
            )
            save_tweet_draft_to_sheets(creds, draft)
            st.success("この動画の下書きをスプレッドシートに保存しました。")
        except Exception as e:
            st.error(f"下書きの保存に失敗しました：{e}")

    st.markdown("#### 📋 コピー用プレビュー（右上のアイコンからコピーできます）")
    if tweet_text:
        st.code(tweet_text, language=None)
    else:
        st.info("上のボタンでツイート文を自動作成するか、自分で入力してください。")

    st.markdown("---")
    st.caption("💡 公開予定の日時を見ながら、X公式の予約投稿フォームで同じ時間に投稿予約を設定してください。")


if __name__ == "__main__":
    main()
