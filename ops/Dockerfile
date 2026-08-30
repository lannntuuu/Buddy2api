FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

WORKDIR /app

# 依赖文件在 ops/requirements/ 下，复制时重命名为镜像内的 requirements.txt，
# 这样 CMD 和 entrypoint 不需要知道仓库里的实际路径。
COPY ops/requirements/base.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 buddy2api \
    && mkdir -p /app/data \
    && chown -R buddy2api:buddy2api /app \
    && chmod +x /app/ops/docker-entrypoint.sh \
    && ln -s /app/ops/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 8787

CMD ["python", "-m", "gateway.server", "--host", "0.0.0.0", "--port", "8787"]
