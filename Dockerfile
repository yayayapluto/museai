FROM python:3.12-alpine

RUN adduser -D -u 1000 muse
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && pip uninstall -y pip setuptools

ENV MUSE2API_HOST=0.0.0.0 \
    MUSE2API_DATA_DIR=/app/data \
    MUSE2API_DRIVER=http

RUN mkdir -p /app/data && chown -R muse:muse /app
USER muse

EXPOSE 18610
CMD ["museai"]
