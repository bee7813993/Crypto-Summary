"""Syncthing クライアントのテスト（HTTP は差し替える）

重点検証:
- デバイス ID の正規化（貼り付け時の空白・改行・区切りの有無を吸収）
- 既に登録済みの相手を二重登録しない
- フォルダ作成時に自分自身を devices に含める（含めないと同期が始まらない）
- 既存フォルダのパス・種別は変えず、共有先だけ足す
- 未設定・未接続を例外にせず状態として返す
"""
import pytest

from crypto_summary.core import syncthing as st

_ID_A = "OVFICYT-2QFXQ7G-U4IPHBY-JCGVWYS-AHN7LCN-6PUI7OA-E4246CJ-KK3YXAJ"
_ID_ME = "7A3E2LP-T5IJYRA-C5OZCME-PD25DSZ-JQZFFE2-RXQU5SQ-YZ6ZLNU-UUSRWAL"


@pytest.fixture
def api(monkeypatch):
    """Syncthing の代わりに応答する最小の偽サーバー。"""
    monkeypatch.setenv(st.API_KEY_ENV, "testkey")
    monkeypatch.setenv(st.URL_ENV, "http://syncthing.invalid")

    state = {
        "devices": [],
        "folders": [],
        "ignores": None,
        "calls": [],
        "pending": {},
        "connections": {},
        "folder_status": {"state": "idle", "globalFiles": 0, "localFiles": 0,
                          "needFiles": 0, "errors": 0, "pullErrors": 0},
        "folder_errors": [],
    }

    def fake_request(method, path, **kwargs):
        state["calls"].append((method, path))
        body = kwargs.get("json")
        if path == "/rest/system/status":
            return {"myID": _ID_ME}
        if path == "/rest/system/version":
            return {"version": "v2.0.16"}
        if path == "/rest/system/connections":
            return {"connections": state["connections"]}
        if path == "/rest/cluster/pending/devices":
            if method == "DELETE":
                state["pending"].pop(kwargs["params"]["device"], None)
                return None
            return state["pending"]
        if path == "/rest/config/devices":
            if method == "POST":
                state["devices"].append(body)
                return None
            return state["devices"]
        if path.startswith("/rest/config/devices/"):
            return None
        if path == "/rest/config/folders":
            if method == "POST":
                state["folders"].append(body)
                return None
            return state["folders"]
        if path.startswith("/rest/config/folders/"):
            for i, f in enumerate(state["folders"]):
                if f["id"] == body["id"]:
                    state["folders"][i] = body
            return None
        if path == "/rest/db/status":
            return state["folder_status"]
        if path == "/rest/folder/errors":
            return {"errors": state["folder_errors"]}
        if path == "/rest/config/defaults/device":
            return {"deviceID": "", "name": "", "autoAcceptFolders": False}
        if path == "/rest/config/defaults/folder":
            return {"id": "", "label": "", "path": "", "type": "sendreceive"}
        if path == "/rest/db/ignores":
            state["ignores"] = body["ignore"]
            return None
        raise AssertionError(f"未対応の呼び出し: {method} {path}")

    monkeypatch.setattr(st, "_request", fake_request)
    return state


# ---- デバイス ID の正規化 ----

@pytest.mark.parametrize("raw", [
    _ID_A,
    _ID_A.lower(),
    _ID_A.replace("-", ""),
    f"  {_ID_A}\n",
    _ID_A.replace("-", " - "),
])
def test_normalize_accepts_common_paste_forms(raw):
    assert st.normalize_device_id(raw) == _ID_A


@pytest.mark.parametrize("raw", ["", "TOO-SHORT", _ID_A[:-1], _ID_A + "X"])
def test_normalize_rejects_bad_ids(raw):
    with pytest.raises(st.SyncthingError) as e:
        st.normalize_device_id(raw)
    assert e.value.code == "invalid_device_id"


# ---- 登録 ----

def test_add_device_registers_once(api):
    st.add_device(_ID_A, "crawler")
    st.add_device(_ID_A, "crawler")
    assert [d["deviceID"] for d in api["devices"]] == [_ID_A]


def test_folder_includes_self_and_peer(api):
    """自分自身を devices に含めないと同期が始まらない。"""
    st.ensure_folder("/data/pbr-outputs", "receiveonly", [_ID_A])

    folder = api["folders"][0]
    assert folder["id"] == st.FOLDER_ID
    assert folder["path"] == "/data/pbr-outputs"
    assert folder["type"] == "receiveonly"
    assert [d["deviceID"] for d in folder["devices"]] == [_ID_ME, _ID_A]


def test_existing_folder_keeps_path_and_type(api):
    """利用者が変えたかもしれないので、既存フォルダの設定は上書きしない。"""
    api["folders"].append({
        "id": st.FOLDER_ID, "path": "/custom", "type": "sendreceive",
        "devices": [{"deviceID": _ID_ME}],
    })

    st.ensure_folder("/data/pbr-outputs", "receiveonly", [_ID_A])

    folder = api["folders"][0]
    assert folder["path"] == "/custom"
    assert folder["type"] == "sendreceive"
    assert [d["deviceID"] for d in folder["devices"]] == [_ID_ME, _ID_A]


def test_pair_sets_ignores_only_when_given(api):
    st.pair(_ID_A, "app", path="/out", folder_type="sendonly",
            ignores=list(st.SEND_IGNORE_PATTERNS))
    assert api["ignores"][-1] == "*"
    assert "!last_crawl.json" in api["ignores"]


def test_pair_without_ignores_leaves_them_alone(api):
    st.pair(_ID_A, "crawler", path="/data/pbr-outputs", folder_type="receiveonly")
    assert api["ignores"] is None


# ---- 画面用の状態 ----

def test_overview_reports_pending_and_connection(api):
    api["pending"][_ID_A] = {"name": "crawler", "time": "2026-08-05T12:00:00Z"}
    api["devices"].append({"deviceID": _ID_A, "name": "crawler"})
    api["connections"][_ID_A] = {"connected": True}

    ov = st.overview("/data/pbr-outputs", "receiveonly")

    assert ov["configured"] and ov["reachable"]
    assert ov["my_device_id"] == _ID_ME
    assert ov["pending_devices"][0]["device_id"] == _ID_A
    assert ov["devices"][0]["connected"] is True
    assert ov["expected_path"] == "/data/pbr-outputs"


def test_overview_when_not_configured(monkeypatch):
    monkeypatch.delenv(st.API_KEY_ENV, raising=False)
    ov = st.overview("/data/pbr-outputs", "receiveonly")
    assert ov["configured"] is False
    assert ov["reachable"] is False


def test_overview_when_unreachable(monkeypatch):
    monkeypatch.setenv(st.API_KEY_ENV, "testkey")

    def boom(*a, **k):
        raise st.SyncthingError("unreachable", "接続できません")

    monkeypatch.setattr(st, "_request", boom)
    ov = st.overview("/data/pbr-outputs", "receiveonly")
    assert ov["configured"] is True
    assert ov["reachable"] is False
    assert ov["error_code"] == "unreachable"


# ---- フォルダの状態（繋がっていても空振りすることがある） ----

def _paired(api, connected=True):
    """相手と繋がっていて、同期フォルダもある状態を作る。"""
    api["devices"].append({"deviceID": _ID_A, "name": "crawler"})
    api["connections"][_ID_A] = {"connected": connected}
    api["folders"].append({
        "id": st.FOLDER_ID, "path": "/data/pbr-outputs", "type": "receiveonly",
        "devices": [{"deviceID": _ID_ME}, {"deviceID": _ID_A}],
    })


def test_overview_reports_folder_state(api):
    _paired(api)
    api["folder_status"].update(
        {"state": "syncing", "globalFiles": 4, "localFiles": 2, "needFiles": 2})

    folder = st.overview("/data/pbr-outputs", "receiveonly")["folder"]

    assert folder["state"] == "syncing"
    assert folder["global_files"] == 4
    assert folder["need_files"] == 2


def test_stalled_when_connected_but_no_files(api):
    """接続しているのに 0 件は、どちらかのフォルダ設定が噛み合っていない印。"""
    _paired(api)

    ov = st.overview("/data/pbr-outputs", "receiveonly")

    assert ov["stalled"] is True
    assert ov["folder"]["stalled"] is True


def test_not_stalled_when_files_are_visible(api):
    _paired(api)
    api["folder_status"]["globalFiles"] = 4

    assert st.overview("/data/pbr-outputs", "receiveonly")["stalled"] is False


def test_not_stalled_when_peer_is_offline(api):
    """未接続なら 0 件でも当然。設定の誤りとは区別する。"""
    _paired(api, connected=False)

    assert st.overview("/data/pbr-outputs", "receiveonly")["stalled"] is False


def test_not_stalled_when_folder_has_errors(api):
    """エラーが出ているなら原因はそちら。別の説明を出さない。"""
    _paired(api)
    api["folder_status"]["errors"] = 1

    assert st.overview("/data/pbr-outputs", "receiveonly")["stalled"] is False


def test_folder_errors_are_reported(api):
    _paired(api)
    api["folder_errors"] = [{"error": "permission denied", "path": "x"}]

    folder = st.overview("/data/pbr-outputs", "receiveonly")["folder"]

    assert folder["messages"] == ["permission denied"]


def test_no_folder_means_no_stall_warning(api):
    api["devices"].append({"deviceID": _ID_A, "name": "crawler"})
    api["connections"][_ID_A] = {"connected": True}

    ov = st.overview("/data/pbr-outputs", "receiveonly")

    assert ov["folder"] is None
    assert ov["stalled"] is False
