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

def extract_snippet(description: str, max_units: int = 250) -> str:
    """
    概要欄からURL/見出し行を除いた短文を生成。最大max_units=250に制限。
    """
    lines = description.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or re.search(r"https?://", s):
            continue
        cleaned.append(s)
    return truncate_to_limit(" ".join(cleaned), max_units=max_units)

def format_publish_at(dt: datetime, tz_name: str = "Asia/Tokyo") -> str:
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    return local.strftime("%Y/%m/%d %H:%M")

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

def ensure_url_and_limit(text: str, url: str, max_units: int = 280) -> str:
    """
    URLは常に保持しつつ280以内に収める最終ガード。
    """
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
    """
    優先度 {url}>{title}>{snippet}>{publish_at} で280以内に調整。
    - まず publish_at を全文から除去（必要時）
    - 次に snippet を縮める（0まで許容、初期上限は250）
    - 次に title を縮める（最小は0）
    - 最後に ensure_url_and_limit でURL以外を切り詰め
    """
    def units(s: str) -> int:
        bu, uu, _ = count_units_breakdown(s)
        return bu + uu

    text = raw

    # すでにOKなら即返す
    if units(text) <= max_units:
        return text

    # 1) publish_at 除去（出現箇所すべて）
    if publish_text and publish_text in text:
        # 前後の余分な空白/改行も極力除去
        pattern = r"[ \t]*" + re.escape(publish_text) + r"[ \t]*"
        text = re.sub(pattern, "", text)
        # 連続改行を1つに
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if units(text) <= max_units:
            return text

    # 2) snippet 短縮
    if snippet_text and snippet_text in text:
        # 現在のスニペットユニット数
        current_snip_units = count_units(snippet_text)
        # 残りを計算： snippet に割り当てられる上限
        over = units(text) - max_units
        target_snip_units = max(0, current_snip_units - over)
        if target_snip_units < current_snip_units:
            new_snip = truncate_to_limit(snippet_text, max_units=target_snip_units)
            text = text.replace(snippet_text, new_snip)
            snippet_text = new_snip  # 後段の計算でも使う
            if units(text) <= max_units:
                return text

    # 3) title 短縮
    if title_text and title_text in text:
        current_title_units = count_units(title_text)
        over = units(text) - max_units
        target_title_units = max(0, current_title_units - over)
        if target_title_units < current_title_units:
            new_title = truncate_to_limit(title_text, max_units=target_title_units)
            text = text.replace(title_text, new_title)
            title_text = new_title
            if units(text) <= max_units:
                return text

    # 4) 最後の砦
    text = ensure_url_and_limit(text, url_text, max_units=max_units)
    return text

def build_tweet_from_template(template_body: str, video: Video, snippet: str, max_units: int = 280) -> str:
    """
    テンプレに差し込み後、優先度ルールで280unit以内に収める。
    """
    # 日付は m月d日(曜) 形式へ
    publish_at_pretty = format_publish_at_pretty(video.publish_at_utc)
    url = video.url
    title = video.title
    # snippetはextract時点で最大250unitに制限済み

    raw = template_body.format(
        title=title,
        url=url,
        snippet=snippet,
        publish_at=publish_at_pretty,
    )
    # 優先度ルールで調整
    tweet = prioritize_and_fit(
        raw=raw,
        url_text=url,
        title_text=title,
        snippet_text=snippet,
        publish_text=publish_at_pretty,
        max_units=max_units,
    )
    return tweet

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
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
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
        pl = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        video_ids += [it["contentDetails"]["videoId"] for it in pl.get("items", [])]
        page_token = pl.get("nextPageToken")
        if not page_token:
            break
    if not video_ids:
        return []

    videos: List[Video] = []
    for i in range(0, len(video_ids), 50):
        resp = youtube.videos().list(part="snippet,status", id=",".join(video_ids[i:i+50])).execute()
        for item in resp.get("items", []):
            status, snip = item.get("status", {}), item.get("snippet", {})
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
            videos.append(Video(
                video_id=item["id"],
                title=snip.get("title", ""),
                description=snip.get("description", ""),
                publish_at_utc=publish_dt,
            ))
    videos.sort(key=lambda v: v.publish_at_utc)
    return videos

# ==============================
# Google Sheets（テンプレ）
# ==============================

def default_templates() -> List[Template]:
    return [
        Template(id="1", name="シンプルなお知らせ", body="【新着】{title}\n\n{snippet}\n\n▼動画はこちら\n{url}", is_default=True),
        Template(id="2", name="丁寧めなお知らせ", body="本日 {publish_at} に動画を公開予定です。\n\n{snippet}\n\n{url}", is_default=False),
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
        spreadsheetId=SPREADSHEET_ID, range="Templates!A2:D"
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
        body={"values": [[template.id, template.name, template.body, "TRUE" if template.is_default else "FALSE"]]},
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

    st.markdown("<style>div[data-testid='stExpander']{margin-bottom:0.75rem}</style>", unsafe_allow_html=True)

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

    creds: Optional[Credentials] = st.session_state["google_creds"]

    # 認証UI
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
            for k in ["google_creds", "videos", "tweet_text",
                      "templates_loaded", "tmpl_editor_name", "tmpl_editor_body",
                      "tmpl_picker", "current_template_id"]:
                st.session_state.pop(k, None)
            st.rerun()

    if creds is None:
        st.info("左のサイドバーから Google アカウント連携を行ってください。")
        return

    # テンプレ読み込み（初回）
    if "templates_loaded" not in st.session_state:
        try:
            st.session_state["templates"] = load_templates_from_sheets(creds)
        except Exception as e:
            st.warning(f"テンプレ読み込みに失敗しました（初期テンプレを使用）：{e}")
        st.session_state["templates_loaded"] = True
    templates: List[Template] = st.session_state["templates"]

    # 予約動画取得
    st.sidebar.header("② YouTube予約動画の取得")
    if st.sidebar.button("自分のチャンネルの予約動画リストを取得／更新"):
        try:
            st.session_state["videos"] = fetch_scheduled_videos(creds)
            if st.session_state["videos"]:
                st.sidebar.success(f"{len(st.session_state['videos'])} 件の予約動画を取得しました。")
            else:
                st.sidebar.warning("予約投稿中の動画が見つかりませんでした。")
        except Exception as e:
            st.sidebar.error(f"予約動画の取得に失敗しました：{e}")

    videos: List[Video] = st.session_state["videos"]
    if not videos:
        st.info("左のサイドバーで「予約動画リストを取得／更新」を実行してください。")
        return

    # 対象動画 & テンプレ選択（サイドバー）
    st.sidebar.header("③ 対象動画とテンプレの選択")
    video_options = {f"{v.title} / {format_publish_at(v.publish_at_utc)}": v.video_id for v in videos}
    selected_label = st.sidebar.selectbox("予約動画を選んでください", list(video_options.keys()))
    current_video = next(v for v in videos if v.video_id == video_options[selected_label])

    tmpl_map = {t.name: t.id for t in templates}
    selected_tmpl_label = st.sidebar.selectbox("テンプレ（ツイートの型）を選んでください", list(tmpl_map.keys()))
    sidebar_selected_template = next(t for t in templates if t.id == tmpl_map[selected_tmpl_label])

    # 初期の「現在のテンプレ」IDをサイドバー選択に合わせる（初回のみ）
    if st.session_state["current_template_id"] is None:
        st.session_state["current_template_id"] = sidebar_selected_template.id

    # メイン：動画情報
    st.subheader("📝 自動生成と編集")
    st.write(f"**動画タイトル：** {current_video.title}")
    st.write(f"**公開予定日時：** {format_publish_at(current_video.publish_at_utc)}")
    st.write(f"**動画URL：** {current_video.url}")

    # 概要欄
    with st.expander("概要欄を確認する"):
        st.text(current_video.description)

    # テンプレ呼び出し（適用のみ）
    with st.popover("📄 テンプレを呼び出す（一覧から選択）", use_container_width=True):
        name_map = {(f"★ {t.name}" if t.is_default else t.name): t.id for t in templates}
        labels_sorted = sorted(name_map.keys(), key=lambda x: (not x.startswith("★ "), x.lower()))
        sel_label = st.radio("テンプレを選んでください", options=labels_sorted, index=0)
        sel_tmpl = next(t for t in templates if t.id == name_map[sel_label])
        st.text_area("プレビュー", value=sel_tmpl.body, height=140, disabled=True)
        if st.button("このテンプレを本文に反映する（自動差し込み）", use_container_width=True, key=f"apply_auto_{sel_tmpl.id}"):
            # ここで「現在のテンプレ」も更新する
            st.session_state["current_template_id"] = sel_tmpl.id
            st.session_state["tmpl_picker"] = sel_tmpl.name
            st.session_state["tmpl_editor_name"] = sel_tmpl.name
            st.session_state["tmpl_editor_body"] = sel_tmpl.body

            snippet = extract_snippet(current_video.description)  # 既に250unit上限
            tweet = build_tweet_from_template(sel_tmpl.body, current_video, snippet)
            st.session_state["tweet_text"] = tweet
            st.success("テンプレ＋差し込みで本文を作成・反映しました。")
            st.rerun()

    # 現在のテンプレ（サイドバー選択ではなく、sessionの current_template_id を採用）
    cur_tmpl = next((t for t in templates if t.id == st.session_state["current_template_id"]), sidebar_selected_template)
    st.markdown("#### 現在のテンプレ")
    st.write(f"**テンプレ名：** {cur_tmpl.name}")
    st.code(cur_tmpl.body or "(本文なし)", language=None)

    # テンプレ編集（呼び出したテンプレ本文を編集）
    with st.expander("🔧 テンプレ編集（選択→内容を編集→保存）"):
        if st.session_state["tmpl_picker"] is None:
            st.session_state["tmpl_picker"] = cur_tmpl.name
            st.session_state["tmpl_editor_name"] = cur_tmpl.name
            st.session_state["tmpl_editor_body"] = cur_tmpl.body

        picker_options = [t.name for t in templates]
        try:
            default_index = picker_options.index(st.session_state["tmpl_picker"])
        except ValueError:
            default_index = picker_options.index(cur_tmpl.name)

        picked = st.selectbox("テンプレを選択", picker_options, index=default_index)

        if picked != st.session_state["tmpl_picker"]:
            st.session_state["tmpl_picker"] = picked
            t = next(t for t in templates if t.name == picked)
            st.session_state["tmpl_editor_name"] = t.name
            st.session_state["tmpl_editor_body"] = t.body
            st.session_state["current_template_id"] = t.id  # エディタ切り替え時も「現在のテンプレ」を同期

        ed_name = st.text_input("テンプレ名", key="tmpl_editor_name")
        ed_body = st.text_area("テンプレ本文", key="tmpl_editor_body", height=160)

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("💾 このテンプレを保存（上書き）"):
                try:
                    target = next(t for t in templates if t.name == st.session_state["tmpl_picker"])
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

        with c2:
            if st.button("➕ このテンプレを新規追加"):
                try:
                    new_id = next_template_id(templates)
                    new_tmpl = Template(id=new_id, name=(ed_name or f"新規テンプレ {new_id}").strip(), body=ed_body, is_default=False)
                    append_template_to_sheets(creds, new_tmpl)
                    st.session_state["templates"] = templates + [new_tmpl]
                    st.session_state["tmpl_picker"] = new_tmpl.name
                    st.session_state["current_template_id"] = new_tmpl.id
                    st.success(f"テンプレを新規追加しました（ID={new_id}）。")
                    st.rerun()
                except Exception as e:
                    st.error(f"新規追加に失敗しました：{e}")

        with c3:
            if st.button("🗑 このテンプレを削除"):
                try:
                    remaining = [t for t in templates if t.name != st.session_state["tmpl_picker"]]
                    if len(remaining) == len(templates):
                        st.warning("削除対象のテンプレが見つかりません。")
                    elif not remaining:
                        st.warning("テンプレは最低1件必要なため、削除できません。")
                    else:
                        save_templates_to_sheets(creds, remaining)
                        st.session_state["templates"] = remaining
                        # フォールバック
                        st.session_state["tmpl_picker"] = remaining[0].name
                        st.session_state["current_template_id"] = remaining[0].id
                        st.success("テンプレを削除しました。")
                        st.rerun()
                except Exception as e:
                    st.error(f"削除に失敗しました：{e}")

        # 現在のテンプレでツイートを再生成
        st.markdown("---")
        if st.button("🌀 現在のテンプレを使用して再出力"):
            snippet = extract_snippet(current_video.description)  # 250unit上限
            tweet = build_tweet_from_template(st.session_state["tmpl_editor_body"], current_video, snippet)
            st.session_state["tweet_text"] = tweet
            st.success("現在のテンプレでツイート本文を再生成しました。")
            st.rerun()

        # 差し込みキーワードの意味（折りたたみ）
        st.markdown("---")
        with st.expander("差し込みキーワードの意味", expanded=False):
            st.markdown("**タイトル**（動画タイトルがそのまま入ります）")
            st.code("{title}", language=None)

            st.markdown("**概要（自動要約）**（概要欄から自動で抜き出した短い説明文が入ります。最大250unit）")
            st.code("{snippet}", language=None)

            st.markdown("**動画URL**（YouTubeの動画URLが入ります。URLは24unit換算）")
            st.code("{url}", language=None)

            st.markdown("**公開日時**（m月d日(曜) 形式で入ります。例：11月12日(水)）")
            st.code("{publish_at}", language=None)

    # ===== ツイート本文 =====
    tweet_text = st.text_area(
        "✏️ ツイート本文（ここで自由に編集できます。改行もそのまま反映されます）",
        key="tweet_text",
        height=240,
    )

    st.info(f"⏰ この動画の公開予定日時： {format_publish_at(current_video.publish_at_utc)}")

    # 文字数カウント
    body_units, url_units, url_count = count_units_breakdown(tweet_text or "")
    total_units = body_units + url_units
    if total_units > 280:
        st.error(f"現在 {total_units}字（本文{body_units}字 + URL{url_units}字 / URL本数 {url_count}）－ 280字を超えています。")
    else:
        st.write(f"現在 **{total_units}字（本文{body_units}字 + URL{url_units}字）** ／ 280字")

    # コピー
    safe_text = (tweet_text or "")
    safe_text = safe_text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'").replace("\n", "\\n")
    html(
        f"""
        <div style="margin: 0.5rem 0 1rem 0;">
          <button
            style="padding:8px 14px;border-radius:8px;border:1px solid #aaa;cursor:pointer;"
            onclick='navigator.clipboard.writeText("{safe_text}")'>
            クリップボードにコピー
          </button>
        </div>
        """,
        height=60,
    )

if __name__ == "__main__":
    main()
