FROM node:26.11.1-bookworm-slim AS static-check-node

WORKDIR /check
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY jsconfig.service-worker.json ./
COPY static/sw.js static/sw.js
RUN ./node_modules/.bin/tsc --project jsconfig.service-worker.json

FROM python:3.11.17-slim-bookworm AS static-check-python

WORKDIR /check
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-coverage.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-coverage.txt
COPY --from=static-check-node /usr/local/bin/node /usr/local/bin/node
COPY --from=static-check-node /check/node_modules/ /check/node_modules/
COPY --from=static-check-node /check/package.json ./
COPY --from=static-check-node /check/jsconfig.service-worker.json ./
COPY --from=static-check-node /check/static/sw.js static/sw.js
COPY pyrightconfig.json ./
COPY jsconfig.speech.json jsconfig.audit.json ./
COPY typings/ typings/
COPY app/ app/
COPY Dockerfile .dockerignore .coveragerc ./
COPY static/ static/
COPY scripts/ scripts/
COPY tests/ tests/
RUN touch tests/__init__.py
ENV APP_ROOT=/check
RUN node node_modules/pyright/index.js --project pyrightconfig.json
RUN node node_modules/typescript/bin/tsc --project jsconfig.audit.json
RUN python -B -m unittest discover -s tests -p test_static_checks.py -v \
 && python -B -m unittest discover -s tests -p test_coverage_gate.py -v \
 && python -B -m unittest discover -s tests -p test_mutation_runner.py -v \
 && node tests/test_service_worker.cjs \
 && python -B -m unittest discover -s tests -p test_speech_static.py -v \
 && node tests/test_speech_plan.cjs \
 && node tests/test_speech_response.cjs \
 && python -B -m unittest discover -s tests -p test_date_static.py -v \
 && node tests/test_date_terms.cjs \
 && python -m coverage run -m unittest -q tests.test_api tests.test_bucket_probe tests.test_bucket_snapshot tests.test_data_loader tests.test_downloads tests.test_indexer tests.test_full_build tests.test_parse_boundaries tests.test_proofread_boundaries tests.test_submission_pause tests.test_reindex_scheduler tests.test_search tests.test_search_store tests.test_stores tests.test_storage_lock tests.test_parse_core \
 && python -m coverage report --fail-under=80

FROM debian:bookworm-slim AS elasticsearch

ARG ES_VERSION=8.19.23
ARG ES_SHA512=ccec1037814ef8051717d09adf22fde4edd93135284eb84948eb8fd82cb4e95513bb8c0780deb555e570fef549968f1d59ebe1ba81683afc6f8fab939ad03e6f

ARG DEBIAN_FRONTEND=noninteractive
ENV ES_HOME=/opt/elasticsearch

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tar \
  && rm -rf /var/lib/apt/lists/*

COPY scripts/install-elasticsearch.sh /usr/local/bin/install-elasticsearch

RUN chmod +x /usr/local/bin/install-elasticsearch \
  && /usr/local/bin/install-elasticsearch

FROM node:26.11.1-bookworm-slim AS static-assets

ARG ESBUILD_VERSION=0.28.2

WORKDIR /build

RUN npm install --global "esbuild@${ESBUILD_VERSION}" \
  && test "$(esbuild --version)" = "${ESBUILD_VERSION}"

COPY static/ /build/static/

RUN mkdir -p /out \
  && esbuild static/app.js --minify --target=es2019 --charset=utf8 --legal-comments=none --outfile=/out/app.js \
  && esbuild static/tts-client.js --minify --target=es2019 --charset=utf8 --legal-comments=none --outfile=/out/tts-client.js \
  && esbuild static/style.css --minify --outfile=/out/style.css \
  && esbuild static/sw.js --minify --target=es2019 --charset=utf8 --legal-comments=none --outfile=/out/sw.js \
  && cp static/index.html /out/index.html

# A stage dependency makes the strict Python and JavaScript checks mandatory for the final image.
COPY --from=static-check-python /check/pyrightconfig.json /tmp/verified-pyrightconfig.json

FROM python:3.11.17-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

ENV ARCHIVE_START=0

ENV ARCHIVE_END=31

ENV ES_HOME=/opt/elasticsearch

ENV ES_JAVA_OPTS="-Xms1g -Xmx1g"

ENV PORT=7860

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    tini \
  && useradd --system --no-create-home --shell /usr/sbin/nologin elasticsearch \
  && rm -rf /var/lib/apt/lists/*

COPY --from=elasticsearch --chown=elasticsearch:elasticsearch /opt/elasticsearch/ /opt/elasticsearch/

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/

COPY --from=static-assets /out/ /app/static/

COPY --chmod=755 scripts/start.sh scripts/wait-for-es.sh /app/scripts/

EXPOSE 7860
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["/app/scripts/start.sh"]
