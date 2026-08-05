FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir -e ".[web]"

# 非 root で動かす。/data は起動時にボリュームがマウントされるので、
# コンテナ側で作って所有者を合わせておく（マウント時に権限が引き継がれる
# 環境と、空ディレクトリのままの環境の両方に備える）。
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown -R app:app /app /data
USER app

EXPOSE 8000

# クラウドのヘルスチェック用。認証不要で応答する。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "crypto_summary.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
