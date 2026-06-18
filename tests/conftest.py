"""テスト共通フィクスチャ。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_master_key(monkeypatch):
    """環境変数 CS_SECRET_KEY をテストから隔離する。

    開発者のシェルに CS_SECRET_KEY が設定されていると、マスター鍵未設定を
    検証するテスト（SecretStore / CLI account）が誤って通過してしまう。
    各テストで一旦削除し、必要なテストは明示的に鍵を渡す。
    """
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
