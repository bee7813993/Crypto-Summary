"""テスト共通フィクスチャ。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """挙動を変える環境変数をテストから隔離する。

    開発者のシェルに CS_SECRET_KEY が設定されていると、マスター鍵未設定を
    検証するテスト（SecretStore / CLI account）が誤って通過してしまう。
    BASE_URL / CS_ROOT_PATH が入っていると、生成するアプリの root_path や
    リダイレクト URI が変わってテストが揺れる。
    各テストで一旦削除し、必要なテストは明示的に設定する。
    """
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("CS_ROOT_PATH", raising=False)


@pytest.fixture(autouse=True)
def _isolate_price_caches(tmp_path, monkeypatch):
    """価格キャッシュを一時ファイルへ逃がす。

    price/pricehist のキャッシュは既定で開発者のホームに書かれる。テストが
    実キャッシュを読み書きすると、結果が実行環境に依存するうえ、当日価格などが
    開発者の手元に焼き込まれてしまう。個別に差し替えているテストもあるが、
    それらは後勝ちで同等の隔離になるため干渉しない。
    """
    from crypto_summary.core import price_history as ph, prices as pr

    monkeypatch.setattr(ph, "_hist_cache_path", lambda: tmp_path / "_pricehist.json")
    monkeypatch.setattr(pr, "_cache_path", lambda: tmp_path / "_prices.json")
