import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request as GoogleAuthRequest

# ==============================
# 設定
# ==============================

# YouTube コメント取得に必要なのは youtube.readonly だけです
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
]

DEFAULT_TZ = "Asia/Tokyo"

# ここが「合流地点」です（既存ジェネレーターの貼り付け欄 key と同じにしてください）
TS_TEXT_KEY = "timestamp_text"

# 時刻行の検出（mm:ss / h:mm:ss / hh:mm:ss も許容）
TIME_LINE_RE = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*(.*)$")

# ==============================
# データ構造
# ==============================

@dataclass
class VideoMeta:
    video_id: str
    title: str
    description: str
    publish_at_utc: datetime

    @property
    def url(self) -> str:
        return f"https://youtu.be/{self.video_id}"


# ==============================
# OAuth
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


def handle_oauth_callback() -> None:
    params = st.experimental_get_query_params()
    if "code" not in params:
        return
    code = params.get("code", [None])[0]
    if not code:
        return

    flow = create_flow()
    flow.fetch_token(code=code)
    st.session_state["google_creds"] = flow.credentials

    # URLのクエリを消して再描画
    st.experimental_set_query_params()
    st.rerun()


def start_google_oauth() -> None:
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
# YouTube API（動画情報・コメント）
# ==============================

def extract_video_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    s = url.strip()

    # 生ID（11文字）っぽければそのまま
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s

    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    m = re.search(r"/shorts/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    m = re.search(r"/live/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    return None


def get_my_channel_id(creds: Credentials) -> Optional[str]:
    creds = ensure_valid_creds(creds)
    youtube = build("youtube", "v3", credentials=creds)
    try:
        resp = youtube.channels().list(part="id", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            return None
        return items[0].get("id")
    except Exception:
        return None


def fetch_video_meta(creds: Credentials, video_id: str) -> Optional[VideoMeta]:
    creds = ensure_valid_creds(creds)
    youtube = build("youtube", "v3", credentials=creds)

    try:
        resp = youtube.videos().list(
            part="snippet,liveStreamingDetails,status",
            id=video_id,
        ).execute()
    except HttpError:
        return None

    items = resp.get("items", [])
    if not items:
        return None

    item = items[0]
    snip = item.get("snippet", {}) or {}
    lsd = item.get("liveStreamingDetails", {}) or {}
    status = item.get("status", {}) or {}

    # ライブ枠は scheduledStartTime を優先、それ以外は status.publishAt → snippet.publishedAt の順
    dt_str = lsd.get("scheduledStartTime") or status.get("publishAt") or snip.get("publishedAt")
    if dt_str:
        try:
            publish_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            publish_dt = datetime.now(timezone.utc)
    else:
        publish_dt = datetime.now(timezone.utc)

    return VideoMeta(
        video_id=video_id,
        title=snip.get("title", ""),
        description=snip.get("description", ""),
        publish_at_utc=publish_dt,
    )


def score_ts_comment(text: str, is_owner: bool, like_count: int) -> Tuple[int, int]:
    """
    スコア, ts_lines を返します
    """
    if not text:
        return 0, 0

    lines = text.splitlines()
    ts_lines = sum(1 for ln in lines if TIME_LINE_RE.match(ln.strip()))
    if ts_lines <= 0:
        return 0, 0

    kw = 0
    if re.search(r"(set\s*list|time\s*stamp|timestamp|セトリ|セットリスト|曲目|song\s*list)", text, re.IGNORECASE):
        kw += 1
    if "✿" in text or "＊" in text:
        kw += 1

    score = ts_lines * 10 + kw * 20 + min(max(like_count, 0), 999)
    if is_owner:
        score += 1000
    return score, ts_lines


def fetch_ts_comment_candidates(
    creds: Credentials,
    video_id: str,
    search_terms: str = "",
    max_pages: int = 3,
) -> List[Dict[str, Any]]:
    """
    固定コメントをAPIで確定できない前提で、候補をスコアリングして上位を返します
    """
    creds = ensure_valid_creds(creds)
    youtube = build("youtube", "v3", credentials=creds)
    my_channel_id = get_my_channel_id(creds)

    out: List[Dict[str, Any]] = []
    page_token = None
    pages = 0

    while pages < max_pages:
        kwargs: Dict[str, Any] = dict(
            part="snippet",
            videoId=video_id,
            maxResults=100,
            order="relevance",
            textFormat="plainText",
        )
        if page_token:
            kwargs["pageToken"] = page_token
        if search_terms.strip():
            kwargs["searchTerms"] = search_terms.strip()

        try:
            resp = youtube.commentThreads().list(**kwargs).execute()
        except HttpError:
            break

        for it in resp.get("items", []):
            sn = (it.get("snippet", {}) or {})
            top = ((sn.get("topLevelComment", {}) or {}).get("snippet", {}) or {})

            text = top.get("textOriginal", "") or ""
            like_count = int(top.get("likeCount", 0) or 0)
            author_ch = (top.get("authorChannelId", {}) or {}).get("value")
            is_owner = bool(my_channel_id and author_ch and author_ch == my_channel_id)

            score, ts_lines = score_ts_comment(text, is_owner=is_owner, like_count=like_count)
            if score <= 0:
                continue

            out.append({
                "text": text,
                "like_count": like_count,
                "is_owner": is_owner,
                "score": score,
                "ts_lines": ts_lines,
            })

        page_token = resp.get("nextPageToken")
        pages += 1
        if not page_token:
            break

    out.sort(key=lambda x: x["score"], reverse=True)

    # 重複っぽいものを軽く間引いて上位10件
    uniq: List[Dict[str, Any]] = []
    seen = set()
    for c in out:
        key = hash(c["text"][:200])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
        if len(uniq) >= 10:
            break

    return uniq


# ==============================
# セットリスト抽出（例：既存パーサがあるなら差し替え可）
# ==============================

def to_local_yyyymmdd(dt: datetime, tz_name: str = DEFAULT_TZ) -> str:
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    return local.strftime("%Y%m%d")


def normalize_time_to_hhmmss(h: Optional[str], m: str, s: str) -> str:
    hh = int(h) if h is not None else 0
    mm = int(m)
    ss = int(s)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def split_title_artist(rest: str) -> Tuple[str, str]:
    """
    よくある区切りで「曲名 / アーティスト」を推定します
    """
    rest = (rest or "").strip()
    if not rest:
        return "", ""

    # 先頭記号除去
    rest = re.sub(r"^[\-\*\u2022・▶▼►]+", "", rest).strip()

    sep_candidates = [" / ", " ／ ", "｜", "|", " - ", " – ", " — ", "：", ":", " by "]
    for sep in sep_candidates:
        if sep in rest:
            left, right = rest.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                # "by" は曲名 by アーティストの想定、それ以外は曲名-アーティストも許容します
                return left, right

    return rest, ""


def build_setlist_df(text: str, video: Optional[VideoMeta]) -> pd.DataFrame:
    if not text:
        return pd.DataFrame()

    rows: List[Dict[str, str]] = []
    publish_yyyymmdd = to_local_yyyymmdd(video.publish_at_utc) if video else ""
    url = video.url if video else ""
    vtitle = video.title if video else ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = TIME_LINE_RE.match(line)
        if not m:
            continue

        hh, mm, ss, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        t = normalize_time_to_hhmmss(hh, mm, ss)

        title, artist = split_title_artist(rest)

        rows.append({
            "publish_yyyymmdd": publish_yyyymmdd,
            "time": t,
            "title": title,
            "artist": artist,
            "url": url,
            "video_title": vtitle,
            "raw": raw,
        })

    return pd.DataFrame(rows)


# ==============================
# 既存「プレビュー以下（合流地点）」をここに集約
# ==============================

def render_preview_and_export(video: Optional[VideoMeta]) -> None:
    """
    ここが「プレビュー以下の流れ（既存に合流させる箇所）」です
    - 入力は st.session_state[TS_TEXT_KEY] だけを見ます
    - 既存のプレビュー/CSV生成/ダウンロードがあるなら、この関数の中身を差し替えるだけで合流できます
    """
    st.markdown("### プレビュー／CSV出力")

    text = st.session_state.get(TS_TEXT_KEY, "") or ""
    if not text.strip():
        st.info("タイムスタンプ情報が空です。")
        return

    if st.button("プレビューを更新"):
        df = build_setlist_df(text, video)
        st.session_state["preview_df"] = df
        st.rerun()

    df: Optional[pd.DataFrame] = st.session_state.get("preview_df")
    if df is None:
        st.caption("「プレビューを更新」を押すと表示されます。")
        return

    if df.empty:
        st.warning("時刻行（例：0:00 / 00:00 / 1:02:03）が見つかりませんでした。")
        return

    st.dataframe(df, use_container_width=True, hide_index=True)

    # CSVダウンロード
    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    fname_date = df.iloc[0].get("publish_yyyymmdd", "") if not df.empty else ""
    fname = f"setlist_{fname_date}.csv" if fname_date else "setlist.csv"

    st.download_button(
        label="CSVをダウンロード",
        data=csv_bytes,
        file_name=fname,
        mime="text/csv",
    )


# ==============================
# UI：入力方式切替（マニュアル / おまかせ）
# ==============================

def render_input_switcher(creds: Credentials) -> Optional[VideoMeta]:
    st.markdown("### タイムスタンプ入力")

    st.session_state.setdefault("ts_input_mode", "マニュアル")
    st.session_state.setdefault(TS_TEXT_KEY, "")

    mode = st.radio(
        "入力方式",
        ["マニュアル", "おまかせ（コメント自動取得）"],
        horizontal=True,
        key="ts_input_mode",
    )

    # 動画URLはどちらでも使うので共通で保持します
    st.session_state.setdefault("ts_video_url", "")
    st.session_state.setdefault("ts_video_id", None)
    st.session_state.setdefault("ts_video_meta", None)

    # 「おまかせ」用
    st.session_state.setdefault("ts_search_terms", "set list")
    st.session_state.setdefault("ts_candidates", [])
    st.session_state.setdefault("ts_pick_idx", 0)

    col_url, col_hint = st.columns([3, 2])
    with col_url:
        st.text_input("動画URL（任意ですが、おまかせは必須です）", key="ts_video_url")
    with col_hint:
        st.caption("shorts/live/watch/youtu.be 全対応です。")

    # 動画メタは URL から引く（おまかせ・マニュアル共通で表示できるようにします）
    video_meta: Optional[VideoMeta] = None
    url = st.session_state.get("ts_video_url", "")
    vid = extract_video_id_from_url(url) if url else None
    if vid:
        st.session_state["ts_video_id"] = vid
        if st.button("動画情報を取得", key="btn_fetch_video_meta"):
            vm = fetch_video_meta(creds, vid)
            st.session_state["ts_video_meta"] = vm
            st.rerun()

    video_meta = st.session_state.get("ts_video_meta")
    if video_meta:
        st.markdown("#### 対象動画（確認）")
        st.write(f"**タイトル：** {video_meta.title}")
        st.write(f"**URL：** {video_meta.url}")
        st.write(f"**日時(UTC)：** {video_meta.publish_at_utc.strftime('%Y-%m-%d %H:%M:%S')}")

    if mode == "おまかせ（コメント自動取得）":
        st.markdown("#### コメントからタイムスタンプ候補を取得")

        col_st, col_btn = st.columns([3, 1])
        with col_st:
            st.text_input("検索語（任意）", key="ts_search_terms")
        with col_btn:
            if st.button("候補を取得", key="btn_fetch_candidates"):
                if not vid:
                    st.error("動画URLから videoId を抽出できませんでした。")
                else:
                    cands = fetch_ts_comment_candidates(
                        creds=creds,
                        video_id=vid,
                        search_terms=st.session_state.get("ts_search_terms", ""),
                        max_pages=3,
                    )
                    st.session_state["ts_candidates"] = cands
                    st.session_state["ts_pick_idx"] = 0
                    if not cands:
                        st.warning("候補が見つかりませんでした（コメント無効/該当なしの可能性）です。")
                    else:
                        st.success(f"{len(cands)} 件の候補を取得しました。")
                st.rerun()

        cands: List[Dict[str, Any]] = st.session_state.get("ts_candidates", []) or []
        if cands:
            labels = []
            for i, c in enumerate(cands):
                head = (c["text"].splitlines()[0] if c["text"] else "").strip()
                head = head[:90] + ("…" if len(head) > 90 else "")
                owner = "主" if c.get("is_owner") else "他"
                labels.append(f"[{i+1}] ts={c.get('ts_lines',0)} like={c.get('like_count',0)} {owner}｜{head}")

            idx = st.selectbox(
                "候補コメント",
                options=list(range(len(labels))),
                format_func=lambda i: labels[i],
                index=int(st.session_state.get("ts_pick_idx", 0) or 0),
            )
            st.session_state["ts_pick_idx"] = idx

            col_apply, col_note = st.columns([1, 3])
            with col_apply:
                if st.button("この候補を入力欄へ反映", key="btn_apply_candidate"):
                    st.session_state[TS_TEXT_KEY] = cands[idx]["text"]
                    st.success("入力欄に反映しました。以降はプレビュー以下が同じ入力で動きます。")
                    st.rerun()
            with col_note:
                st.caption("反映後も下の入力欄で自由に編集できます。")

    # 合流地点：マニュアルでもおまかせでも、ここに最終テキストが入る設計です
    st.text_area(
        "タイムスタンプ情報（合流入力）",
        key=TS_TEXT_KEY,
        height=280,
        placeholder="ここにタイムスタンプ／セットリストを貼り付けます（おまかせ反映もここに入ります）。",
    )

    return video_meta


# ==============================
# メイン
# ==============================

def main() -> None:
    st.set_page_config(page_title="歌枠セットリストCSVジェネレーター", layout="wide")
    st.title("🎤 歌枠セットリストCSVジェネレーター（入力切替：マニュアル／おまかせ）")

    # OAuthコールバック
    handle_oauth_callback()

    st.session_state.setdefault("google_creds", None)

    st.subheader("① Googleアカウント連携")
    creds: Optional[Credentials] = st.session_state.get("google_creds")

    if creds is None:
        st.write("まずは Google アカウントと連携してください。")
        start_google_oauth()
        st.stop()

    creds = ensure_valid_creds(creds)
    if creds is None:
        st.error("認証情報が無効です。認証をやり直してください。")
        st.session_state["google_creds"] = None
        st.rerun()

    st.success("Google認証済みです。")

    st.subheader("② 入力（マニュアル／おまかせ）")
    video_meta = render_input_switcher(creds)

    st.subheader("③ プレビュー以下（合流）")
    render_preview_and_export(video_meta)


if __name__ == "__main__":
    main()
