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
