FROM node:26.13.0-alpine@sha256:8996bb5821d62a8fd9bb27c1cb07462f3fdce6e47d640c6a881ead0b064ce216 AS frontend

WORKDIR /build
RUN apk add --no-cache git && npm install --global --no-audit pnpm@11.20.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY . .

FROM frontend AS managed-ui
ENV VITE_MANAGED_PANEL=1
RUN pnpm type-check && pnpm build

FROM frontend AS standalone-ui
RUN pnpm build

FROM caddy:2.11.9-alpine@sha256:d0ccb617b4f64b7696f1fe050b303460935a9f373cae0a12b18d02e2f924f5f7 AS webserver

FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS managed
RUN apk add --no-cache ca-certificates curl python3 py3-yaml py3-jsonschema tzdata
COPY --from=webserver /usr/bin/caddy /usr/bin/caddy
COPY --from=managed-ui /build/dist/ /srv/
COPY panel/Caddyfile.managed /etc/caddy/Caddyfile
COPY panel/*.py /opt/stack/panel/
COPY panel/panelctl /usr/local/bin/panelctl
COPY tools/*.py /opt/stack/tools/
COPY schemas /opt/stack/schemas/
COPY rules /opt/stack/rules/
ENV PYTHONPATH=/opt/stack PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 XDG_DATA_HOME=/tmp/caddy-data
WORKDIR /opt/stack
CMD ["python3", "-m", "panel.manager"]

FROM webserver AS standalone
EXPOSE 80
WORKDIR /srv
COPY --from=standalone-ui /build/dist/. .
COPY Caddyfile .
CMD ["caddy", "run"]
