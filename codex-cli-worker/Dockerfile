# Base image pinned by digest (fatherdoctor fork, CG-6): the multi-arch index that
# ghcr.io/home-assistant/base:latest pointed to on 2026-11-06. Moving it is a release of ours.
ARG BUILD_FROM=ghcr.io/home-assistant/base:latest@sha256:58422480a299b16b6ea376c2167463206d430619ed02b40cd687c8bd6875519e
FROM $BUILD_FROM

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64
# Codex CLI pinned (fatherdoctor fork, CG-6): the packaged build from OpenAI's GitHub release,
# checked against these SHA-256 sums. The npm build cannot run `codex remote-control`.
ARG CODEX_VERSION=0.166.0
ENV ADDON_VERSION=${BUILD_VERSION} CODEX_VERSION=${CODEX_VERSION}
ARG CODEX_SHA256_AMD64=1d44b28bc4ac9937d7d3532264baa68a507ce364956a0d4ebb856f331cdee254
ARG CODEX_SHA256_AARCH64=7821b77dda2e6283fb1f63f3165177a271da8833e857cf673c6ca1c9db3bfcc6

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
    bash \
    bubblewrap \
    ca-certificates \
    chromium \
    font-noto \
    coreutils \
    curl \
    git \
    jq \
    nodejs \
    npm \
    openssh-client-default \
    python3 \
    py3-pip \
    rsync \
    tar \
    util-linux-misc \
    tzdata

RUN install -D -m 0755 /usr/bin/bwrap /opt/codex-sandbox/bwrap.exec \
    && install -m 0755 /usr/bin/bwrap /opt/codex-sandbox/relay-bwrap

COPY relay-launcher /opt/codex-sandbox/relay-launcher
COPY relay-tool-launcher /opt/codex-sandbox/bwrap.real
COPY codex-bwrap /opt/codex-sandbox/bwrap
COPY codex-bwrap-probe.py /opt/codex-sandbox/codex-bwrap-probe.py

RUN chmod 0555 /opt/codex-sandbox/bwrap /opt/codex-sandbox/relay-launcher /opt/codex-sandbox/bwrap.real /opt/codex-sandbox/bwrap.exec /opt/codex-sandbox/relay-bwrap

ENV PATH="/opt/codex-sandbox:${PATH}"

# The package unpacks to /opt/codex/<version>, root-owned and read-only, so the CLI can never
# replace itself (self-update is also off in config.toml); a CLI move is a new release of ours.
# `current` is a relative symlink, so the package resolves inside the image (the lab's installer
# wrote an absolute one). The package's own bwrap is pointed at the HAOS wrapper above, so every
# sandbox start goes through the one path the readiness probe checks.
RUN case "${BUILD_ARCH}" in \
        amd64) triple=x86_64-unknown-linux-musl; sum="${CODEX_SHA256_AMD64}" ;; \
        aarch64) triple=aarch64-unknown-linux-musl; sum="${CODEX_SHA256_AARCH64}" ;; \
        *) echo "unsupported arch ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/codex-package.tar.gz \
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-package-${triple}.tar.gz" \
    && echo "${sum}  /tmp/codex-package.tar.gz" | sha256sum -c - \
    && mkdir -p "/opt/codex/${CODEX_VERSION}" \
    && tar -xzf /tmp/codex-package.tar.gz -C "/opt/codex/${CODEX_VERSION}" --no-same-owner \
    && rm -f /tmp/codex-package.tar.gz \
    && test -x "/opt/codex/${CODEX_VERSION}/bin/codex" \
    && ln -sf /opt/codex-sandbox/bwrap "/opt/codex/${CODEX_VERSION}/codex-resources/bwrap" \
    && ln -s "${CODEX_VERSION}" /opt/codex/current \
    && ln -sf /opt/codex/current/bin/codex /usr/local/bin/codex \
    && chmod -R a-w "/opt/codex/${CODEX_VERSION}" \
    && test "$(command -v codex)" = /usr/local/bin/codex \
    && /usr/local/bin/codex --version | grep -F "${CODEX_VERSION}"

# Alpine packages Chromium for both supported architectures. Use its browser,
# rather than downloading a glibc-only Playwright build into the image.
RUN npm install --prefix /opt/verification --omit=dev playwright-core@1.63.0
ENV NODE_PATH=/opt/verification/node_modules

RUN pip3 install --no-cache-dir --break-system-packages \
    flask \
    pyyaml \
    qrcode \
    requests \
    websocket-client

WORKDIR /data

COPY run.sh /
COPY server.py account_access.py /
COPY interaction_diagnostics.py diagnostics_scrub.py /
COPY verbose_log.py /
# The remote-control command check (CX-B6b): managed Codex requirements and hook, read-only, outside
# /config and outside every sandbox's writable roots. The marker goes last: remote control can be
# turned on only when it and every file it lists are present and the hook passes its self-test.
COPY gate/requirements.toml gate/managed_config.toml /etc/codex/
COPY gate/gate_hook.py gate_rules.py policy/refused-domains.json /usr/share/ha_codex/
COPY gate/remote-control-gate.json /usr/share/ha_codex/remote-control-gate.json
RUN chmod 0644 /etc/codex/requirements.toml /etc/codex/managed_config.toml /usr/share/ha_codex/* \
    && chown -R root:root /etc/codex /usr/share/ha_codex

COPY core_address.py relay_reads.py relay_reads_mcp.py scheduling.py schedule_api.py scheduling_mcp.py approvals.py policy.py gate_rules.py remote_control.py relay_diagnostics.py verification.py verification_mcp.py browser_permissions.py browser_permission_policy.json ha_data.py ha_data_mcp.py browser.cjs browser_resources.cjs browser_permissions.cjs /
COPY web /web
# The protected permission policy: read-only, outside /config and every sandbox's writable roots.
COPY policy/policy.json /usr/share/ha_codex/policy.json
RUN chmod 0444 /usr/share/ha_codex/policy.json

RUN chmod a+x /run.sh /server.py

CMD ["/run.sh"]
