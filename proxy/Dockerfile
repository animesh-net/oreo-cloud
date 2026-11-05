# Caddy with the Cloudflare DNS module, for the Pi's proxy. Built and published by the Image
# workflow as ghcr.io/animesh-net/oreo-cloud-proxy. Floating major tags, so every build gets
# the current Caddy and Go; a pinned release falls behind on Go security fixes.
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
