#!/usr/bin/env python3
"""完成した動画を YouTube にアップロードし、予約投稿にする（YouTube Data API v3）。

使う権限は youtube.upload だけ（動画のアップロード・サムネイル設定・公開日時の予約）。
鍵のファイル: config/client_secret.json（Google Cloud の「デスクトップアプリ」のクライアント）
許可の記録:   config/youtube_token.json（初回の許可で作られる。人に渡さない）

  python3 pipeline/youtube_upload.py --auth                 初回だけ：ブラウザで許可する
  python3 pipeline/youtube_upload.py episodes/ep02          送る内容の確認だけ（通信しない）
  python3 pipeline/youtube_upload.py episodes/ep02 --yes    実際にアップロードして予約する

エピソードのフォルダから読むもの:
  config/upload.json        final_title, tags, schedule, made_for_kids, channel.id
  upload/description.txt    概要欄
  output/final.mp4          本編
  thumbnails/thumb_a.png    サムネイル（A案）
結果: upload/upload_result.json（動画ID・URL・予約日時）

※審査（監査）が通るまでは、API で上げた動画は YouTube 側で「非公開」に固定される。
※固定コメント・サムネイルのA/Bテストは API ではできないので、YouTube Studio で行う。
"""
import json
import sys
import time
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

CODE_DIR = Path(__file__).resolve().parent.parent
SECRET = CODE_DIR / "config" / "client_secret.json"
TOKEN = CODE_DIR / "config" / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_IDS = {"エンターテインメント": "24", "映画とアニメ": "1", "ブログ": "22", "教育": "27"}
JST = timezone(timedelta(hours=9))


def credentials(interactive=False, open_browser=True, fresh=False):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = None
    if TOKEN.exists() and not fresh:
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_token(creds)
            return creds
        except Exception as e:
            print("許可の更新に失敗しました（許可し直しが必要です）:", e)
    if not interactive:
        raise SystemExit("まだ許可されていません。先に  python3 pipeline/youtube_upload.py --auth  を実行してください。")
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not SECRET.exists():
        raise SystemExit("config/client_secret.json がありません。")
    flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
    print("ブラウザが開きます。人生漫画のチャンネルのアカウントを選び、「許可」を押してください。")
    print("（チャンネルを選ぶ画面が出たら「あの日の選択【人生漫画】」を選ぶ）")
    creds = flow.run_local_server(port=0, prompt="consent select_account", open_browser=open_browser,
                                  authorization_prompt_message="" if open_browser else "許可のURL: {url}\n", success_message="許可できました。このタブは閉じて大丈夫です。")
    save_token(creds)
    print("許可を保存しました:", TOKEN.relative_to(CODE_DIR))
    return creds


def save_token(creds):
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    TOKEN.chmod(0o600)


def build_request(ep: Path):
    up = json.loads((ep / "config" / "upload.json").read_text(encoding="utf-8"))
    desc = (ep / "upload" / "description.txt").read_text(encoding="utf-8").strip()
    video = ep / "output" / "final.mp4"
    thumb = ep / "thumbnails" / "thumb_a.png"
    title = up.get("final_title") or up["titles"][0]
    sch = up["schedule"]
    local = datetime.strptime(sch["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    publish_at = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    tags = up.get("tags", [])
    body = {
        "snippet": {
            "title": title,
            "description": desc,
            "tags": tags,
            "categoryId": CATEGORY_IDS.get(up.get("category", ""), "24"),
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": "private",          # 予約投稿は「非公開＋公開日時」で指定する
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": bool(up.get("made_for_kids", False)),
            "containsSyntheticMedia": True,      # AIを使った制作であることの申告
            "embeddable": True,
            "publicStatsViewable": True,
        },
    }
    problems = []
    if not video.exists() or video.stat().st_size == 0:
        problems.append(f"動画がありません: {video}")
    if not thumb.exists() or thumb.stat().st_size == 0:
        problems.append(f"サムネイルがありません: {thumb}")
    elif thumb.stat().st_size > 2 * 1024 * 1024:
        problems.append(f"サムネイルが2MBを超えています（{thumb.stat().st_size / 1e6:.1f}MB）")
    if len(title) > 100:
        problems.append(f"タイトルが100文字を超えています（{len(title)}文字）")
    if len(desc.encode("utf-8")) > 5000:
        problems.append(f"概要欄が長すぎます（{len(desc.encode('utf-8'))}バイト／上限5000）")
    if sum(len(t) + 2 for t in tags) > 500:
        problems.append("タグの合計が500文字を超えています")
    if local <= datetime.now(JST) + timedelta(minutes=30):
        problems.append(f"予約日時が近すぎるか過ぎています: {sch['datetime']}")
    return {"body": body, "video": video, "thumb": thumb, "local": local,
            "channel_id": up.get("channel", {}).get("id"), "problems": problems}


def show(req):
    b = req["body"]
    size = req["video"].stat().st_size / 1e6 if req["video"].exists() else 0
    print("―― 送る内容 ――")
    print("タイトル  :", b["snippet"]["title"])
    print("予約日時  :", req["local"].strftime("%Y-%m-%d %H:%M"), "（日本時間）")
    print("動画      :", req["video"].name, f"{size:.0f}MB")
    print("サムネイル:", req["thumb"].name)
    print("タグ      :", "、".join(b["snippet"]["tags"]))
    print("カテゴリ  :", b["snippet"]["categoryId"], "／ 子ども向け:", "はい" if b["status"]["selfDeclaredMadeForKids"] else "いいえ",
          "／ AI使用の申告: あり")
    print("概要欄    :", len(b["snippet"]["description"]), "文字")
    print("チャンネル:", req["channel_id"], "（アップロード後に一致するか確認）")
    for p in req["problems"]:
        print("⚠", p)


def upload(req, ep: Path):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    yt = build("youtube", "v3", credentials=credentials(), cache_discovery=False)
    media = MediaFileUpload(str(req["video"]), mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True)
    call = yt.videos().insert(part="snippet,status", body=req["body"], media_body=media, notifySubscribers=True)
    res, tries = None, 0
    print("アップロード中…")
    while res is None:
        try:
            status, res = call.next_chunk()
            if status:
                print(f"  {status.progress() * 100:.0f}%")
            tries = 0
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1
                time.sleep(2 ** tries)
                continue
            raise
    vid = res["id"]
    print("アップロード完了: https://youtu.be/" + vid)
    got = res.get("snippet", {}).get("channelId")
    if req["channel_id"] and got != req["channel_id"]:
        print(f"⚠⚠ 別のチャンネルに上がりました（{got}）。動画は非公開のままです。YouTube Studio で削除し、"
              "--auth で正しいチャンネルを選んで許可し直してください。")
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(req["thumb"]), mimetype="image/png")).execute()
        print("サムネイルを設定しました")
        thumb_ok = True
    except HttpError as e:
        print("⚠ サムネイルの設定に失敗しました（YouTube Studio で手動で設定してください）:", e)
        thumb_ok = False
    st = res.get("status", {})
    result = {"video_id": vid, "url": "https://youtu.be/" + vid,
              "studio_url": f"https://studio.youtube.com/video/{vid}/edit",
              "channel_id": got, "channel_ok": got == req["channel_id"],
              "privacy": st.get("privacyStatus"), "publish_at": st.get("publishAt"),
              "upload_status": st.get("uploadStatus"), "thumbnail_set": thumb_ok,
              "uploaded_at": datetime.now(JST).isoformat(timespec="seconds")}
    (ep / "upload").mkdir(exist_ok=True)
    (ep / "upload" / "upload_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("予約日時（YouTube側）:", st.get("publishAt"))
    print("残りの作業：YouTube Studio で、サムネイルのA/Bテスト（B案の追加）と、公開後の固定コメント")
    print("  ", result["studio_url"])
    return result


def main():
    args = sys.argv[1:]
    if "--auth" in args:
        credentials(interactive=True, open_browser="--no-browser" not in args, fresh=True)
        return
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return
    ep = (CODE_DIR / paths[0]).resolve()
    req = build_request(ep)
    show(req)
    if req["problems"]:
        raise SystemExit("問題があるので、アップロードしません。")
    if "--yes" not in args:
        print("\n確認だけしました（通信していません）。実際に送るには --yes を付けて実行します。")
        return
    upload(req, ep)


if __name__ == "__main__":
    main()
