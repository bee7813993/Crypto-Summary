"""Syncthing REST API クライアント（ファイル同期のペアリング用）

PBR Lending のクロール結果は、クローラーを動かす機械から Crypto-Summary へ
ファイル同期で運ぶ。その同期相手の登録を、Syncthing の GUI を触らずに
アプリの画面から済ませるための薄いクライアント。

Syncthing の繋ぎ方:
  1. 双方が相手のデバイス ID を登録する（片方だけでは繋がらない）。
     片側が登録すると、もう片側には「承認待ちのデバイス」として現れる。
     「どんな相手でも自動承認」という設定は Syncthing に無い。
  2. フォルダは両側で同じフォルダ ID を使う。ローカルのパスと種別
     （送信のみ／受信のみ）はそれぞれの側で決める。

このモジュールは 1 の登録・承認と 2 のフォルダ作成を行う。
接続先は環境変数 SYNCTHING_URL / SYNCTHING_API_KEY で指定する。

API キーは Syncthing 全体（全フォルダ・全デバイス）を操作できる。渡すのは
その Syncthing を持つ側のアプリだけにすること。
"""
from __future__ import annotations

import os
from typing import Any

#: 接続先。別コンテナで動かす場合はサービス名（http://syncthing:8384）。
URL_ENV = "SYNCTHING_URL"
API_KEY_ENV = "SYNCTHING_API_KEY"
DEFAULT_URL = "http://127.0.0.1:8384"

#: 同期フォルダのパスを *Syncthing から見た形* で上書きする。
#: Syncthing を別コンテナで動かすとマウント位置が違うことがあり、
#: アプリ側の PBR_CRAWL_DIR をそのまま渡すと存在しないパスになる。
#: 未設定なら PBR_CRAWL_DIR と同じ（同じ場所に見えている構成）とみなす。
FOLDER_PATH_ENV = "SYNCTHING_FOLDER_PATH"

#: 同期に使うフォルダ ID。両側で同じ値を使う必要がある。
#: 名前ではなく ID で結び付くので、固定値にしておくと設定が噛み合う。
FOLDER_ID = "pbr-lending-outputs"
FOLDER_LABEL = "PBR Lending outputs"

#: 取り込みに使うファイルだけを同期する除外設定（送信側に入れる）。
#: captures/ は生のスクレイピング結果で容量が大きく、取り込みには使わない。
SEND_IGNORE_PATTERNS: tuple[str, ...] = (
    "!pbrlending_crawled.latest.json",
    "!last_crawl.json",
    "!viewer_transfers.json",
    "!viewer_ledger.json",
    "*",
)

_TIMEOUT = 10


class SyncthingError(Exception):
    """Syncthing に繋がらない、または操作が失敗した。

    code: not_configured | unreachable | unauthorized | api_error | invalid_device_id
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_configured() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


def folder_path(local_path: str) -> str:
    """同期フォルダのパスを Syncthing から見た形で返す。

    Syncthing が別コンテナのときは、こちらの取り込み先とマウント位置が
    違うことがある。その場合は環境変数で上書きする。
    """
    return os.environ.get(FOLDER_PATH_ENV, "").strip() or local_path


def _base_url() -> str:
    return (os.environ.get(URL_ENV, "").strip() or DEFAULT_URL).rstrip("/")


def _request(method: str, path: str, **kwargs) -> Any:
    import httpx

    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise SyncthingError(
            "not_configured",
            f"Syncthing の API キーが未設定です（環境変数 {API_KEY_ENV}）。",
        )
    url = f"{_base_url()}{path}"
    try:
        resp = httpx.request(
            method, url, headers={"X-API-Key": api_key}, timeout=_TIMEOUT, **kwargs
        )
    except httpx.HTTPError as e:
        raise SyncthingError(
            "unreachable", f"Syncthing に接続できません（{_base_url()}）: {e}"
        )
    if resp.status_code in (401, 403):
        raise SyncthingError("unauthorized", "Syncthing の API キーが正しくありません。")
    if resp.status_code >= 400:
        raise SyncthingError(
            "api_error",
            f"Syncthing への操作が失敗しました（HTTP {resp.status_code}）: "
            f"{resp.text[:200]}",
        )
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


# ---------------------------------------------------------------------------
# 読み取り
# ---------------------------------------------------------------------------

def my_device_id() -> str:
    return str(_request("GET", "/rest/system/status").get("myID", ""))


def version() -> str:
    return str(_request("GET", "/rest/system/version").get("version", ""))


def devices() -> list[dict]:
    return list(_request("GET", "/rest/config/devices") or [])


def folders() -> list[dict]:
    return list(_request("GET", "/rest/config/folders") or [])


def pending_devices() -> dict:
    """まだ登録していない相手からの接続要求。{deviceID: {...}}"""
    return dict(_request("GET", "/rest/cluster/pending/devices") or {})


def connections() -> dict:
    """接続状況。{"connections": {deviceID: {connected: bool, ...}}, ...}"""
    return dict(_request("GET", "/rest/system/connections") or {})


def folder_status() -> dict:
    """同期フォルダの状態。デバイスが繋がっていても、フォルダが空振りして
    いることがある（典型はパスの指定違い）ので、件数まで見えるようにする。"""
    return dict(_request(
        "GET", "/rest/db/status", params={"folder": FOLDER_ID}) or {})


def folder_errors() -> list[dict]:
    """同期フォルダのエラー一覧（無ければ空）。"""
    data = _request(
        "GET", "/rest/folder/errors", params={"folder": FOLDER_ID}) or {}
    return list(data.get("errors") or [])


def _default(kind: str) -> dict:
    """新規オブジェクトのテンプレート（Syncthing の版差を吸収する）。"""
    return dict(_request("GET", f"/rest/config/defaults/{kind}") or {})


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------

#: デバイス ID は 7 文字 × 8 組（ハイフン込み 63 文字）。
_ID_GROUP = 7
_ID_GROUPS = 8
_ID_LEN = _ID_GROUP * _ID_GROUPS


def normalize_device_id(device_id: str) -> str:
    """入力されたデバイス ID を検証して整える。

    貼り付け時に混じる空白・改行と、区切りハイフンの有無を吸収する。
    """
    raw = "".join(str(device_id).split()).upper().replace("-", "")
    if len(raw) != _ID_LEN or not raw.isalnum():
        raise SyncthingError(
            "invalid_device_id",
            f"デバイス ID の形式が正しくありません（{_ID_GROUP} 文字 × {_ID_GROUPS} 組）。",
        )
    return "-".join(
        raw[i:i + _ID_GROUP] for i in range(0, _ID_LEN, _ID_GROUP)
    )


def add_device(device_id: str, name: str) -> dict:
    """相手のデバイスを登録する。既に居れば名前だけ更新する。"""
    device_id = normalize_device_id(device_id)
    for existing in devices():
        if existing.get("deviceID") == device_id:
            if name and existing.get("name") != name:
                existing["name"] = name
                _request("PUT", f"/rest/config/devices/{device_id}", json=existing)
            return existing
    device = _default("device")
    device.update({"deviceID": device_id, "name": name or device_id[:7]})
    _request("POST", "/rest/config/devices", json=device)
    return device


def ensure_folder(path: str, folder_type: str, share_with: list[str]) -> dict:
    """同期フォルダを用意し、相手と共有する。

    folder_type: "sendonly"（クローラー側）/ "receiveonly"（アプリ側）
    既にあれば共有先の追加だけ行い、パスや種別は変えない
    （利用者が意図して変えている可能性があるため）。
    """
    wanted = [{"deviceID": normalize_device_id(d)} for d in share_with]

    for folder in folders():
        if folder.get("id") != FOLDER_ID:
            continue
        have = {d.get("deviceID") for d in folder.get("devices") or []}
        added = [d for d in wanted if d["deviceID"] not in have]
        if added:
            folder["devices"] = list(folder.get("devices") or []) + added
            _request("PUT", f"/rest/config/folders/{FOLDER_ID}", json=folder)
        return folder

    folder = _default("folder")
    folder.update({
        "id": FOLDER_ID,
        "label": FOLDER_LABEL,
        "path": path,
        "type": folder_type,
        # 自分自身も devices に含める必要がある
        "devices": [{"deviceID": my_device_id()}] + wanted,
    })
    _request("POST", "/rest/config/folders", json=folder)
    return folder


def set_ignores(patterns: list[str]) -> None:
    """同期フォルダの除外設定を書く（送信側で使う）。"""
    _request(
        "POST", "/rest/db/ignores", params={"folder": FOLDER_ID},
        json={"ignore": list(patterns)},
    )


def dismiss_pending_device(device_id: str) -> None:
    """承認待ちの相手を却下する。"""
    _request(
        "DELETE", "/rest/cluster/pending/devices",
        params={"device": normalize_device_id(device_id)},
    )


# ---------------------------------------------------------------------------
# まとめて行う操作
# ---------------------------------------------------------------------------

def pair(
    device_id: str, name: str, *, path: str, folder_type: str,
    ignores: list[str] | None = None,
) -> dict:
    """相手を登録し、同期フォルダを用意して共有する。

    片側でこれを行うと、もう片側には承認待ちとして現れる。
    もう片側でも同じ操作（承認）をすると繋がる。
    """
    device_id = normalize_device_id(device_id)
    add_device(device_id, name)
    folder = ensure_folder(path, folder_type, [device_id])
    if ignores:
        set_ignores(ignores)
    return {
        "device_id": device_id,
        "folder_id": FOLDER_ID,
        "folder_path": folder.get("path"),
        "folder_type": folder.get("type"),
    }


def overview(path: str, folder_type: str) -> dict:
    """画面表示用の状態をまとめて返す。

    未設定・未接続でも例外にせず、状態として返す（画面が壊れないように）。
    """
    if not is_configured():
        return {"configured": False, "url": _base_url(), "reachable": False}

    try:
        me = my_device_id()
        ver = version()
    except SyncthingError as e:
        return {
            "configured": True, "url": _base_url(), "reachable": False,
            "error_code": e.code, "error": e.message,
        }

    conns = (connections() or {}).get("connections") or {}
    known = [
        {
            "device_id": d.get("deviceID"),
            "name": d.get("name"),
            "connected": bool((conns.get(d.get("deviceID")) or {}).get("connected")),
        }
        for d in devices() if d.get("deviceID") != me
    ]
    pending = [
        {"device_id": did, "name": info.get("name") or "", "time": info.get("time")}
        for did, info in (pending_devices() or {}).items()
    ]
    folder = next((f for f in folders() if f.get("id") == FOLDER_ID), None)

    status: dict = {}
    if folder is not None:
        try:
            raw = folder_status()
            status = {
                "state": raw.get("state"),
                "global_files": raw.get("globalFiles", 0),
                "local_files": raw.get("localFiles", 0),
                "need_files": raw.get("needFiles", 0),
                "global_bytes": raw.get("globalBytes", 0),
                "errors": raw.get("errors", 0) or raw.get("pullErrors", 0),
                "messages": [
                    e.get("error") for e in folder_errors() if e.get("error")
                ][:3],
            }
        except SyncthingError:
            # 状態が取れなくても、他の情報は出せるようにする
            status = {}

    # 相手と繋がっているのに 1 件も見えていない = どちらかのフォルダ設定が
    # 噛み合っていない（多くはパスの指定違い）。気づけるように印を付ける。
    any_connected = any(d["connected"] for d in known)
    status["stalled"] = bool(
        folder is not None and any_connected
        and status.get("global_files", 0) == 0
        and not status.get("errors")
    )

    return {
        "configured": True,
        "url": _base_url(),
        "reachable": True,
        "version": ver,
        "my_device_id": me,
        "devices": known,
        "pending_devices": pending,
        "folder_id": FOLDER_ID,
        "folder": None if folder is None else {
            "path": folder.get("path"),
            "type": folder.get("type"),
            "shared_with": [
                d.get("deviceID") for d in folder.get("devices") or []
                if d.get("deviceID") != me
            ],
            **status,
        },
        "stalled": status.get("stalled", False),
        # 画面に出す「あるべき設定」。実際の folder と食い違えば警告できる。
        "expected_path": path,
        "expected_type": folder_type,
    }
