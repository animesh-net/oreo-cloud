# Caddy with the Cloudflare DNS module, for the Pi's proxy. Built and published by the Image
# workflow as ghcr.io/animesh-net/oreo-cloud-proxy; Dependabot moves the version pins.
FROM caddy:2.10.0-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2.10.0
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
