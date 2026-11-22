FROM node:26.13.0-alpine@sha256:8996bb5821d62a8fd9bb27c1cb07462f3fdce6e47d640c6a881ead0b064ce216 AS builder

WORKDIR /build

RUN apk add --no-cache git && npm install --global --no-audit pnpm@11.20.0

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY . .
RUN pnpm build

FROM caddy:2.11.9-alpine@sha256:d0ccb617b4f64b7696f1fe050b303460935a9f373cae0a12b18d02e2f924f5f7

EXPOSE 80

WORKDIR /srv

COPY --from=builder /build/dist/. .
COPY Caddyfile .

CMD ["caddy", "run"]
