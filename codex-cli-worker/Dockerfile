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

RUN pip3 install --no-cache-dir --break-system-packages \
    flask \
    pyyaml \
    qrcode \
    requests \
    websocket-client

# The Home Assistant MCP server (option home_assistant_mcp), pinned with every package it needs and the SHA-256
# of each wheel for amd64 and aarch64, in its own virtual environment so it cannot clash with the packages above.
# Wheels only: nothing is compiled. The venv is read-only; hass_mcp.py says which tools Codex is offered.
ARG HASS_MCP_VERSION=0.6.0
COPY hass-mcp.requirements.txt /usr/share/ha_codex/hass-mcp.requirements.txt
RUN grep -qF "hass-mcp==${HASS_MCP_VERSION} " /usr/share/ha_codex/hass-mcp.requirements.txt \
    && python3 -m venv --without-pip /opt/hass-mcp \
    && pip3 --python /opt/hass-mcp/bin/python3 install --no-cache-dir --require-hashes --no-deps \
        --only-binary=:all: -r /usr/share/ha_codex/hass-mcp.requirements.txt \
    && test -x /opt/hass-mcp/bin/hass-mcp \
    && /opt/hass-mcp/bin/python3 -c "import importlib.metadata as m; assert m.version('hass-mcp') == '${HASS_MCP_VERSION}'" \
    && chmod -R a-w /opt/hass-mcp \
    && chmod 0444 /usr/share/ha_codex/hass-mcp.requirements.txt
COPY hass_mcp_launch.py /usr/share/ha_codex/hass_mcp_launch.py
RUN chmod 0555 /usr/share/ha_codex/hass_mcp_launch.py && chown root:root /usr/share/ha_codex/hass_mcp_launch.py

WORKDIR /data

COPY run.sh /
COPY server.py account_access.py safety_block.py codex_host.py starter_agents.md /
COPY interaction_diagnostics.py diagnostics_scrub.py /
# Codex's managed requirements (the sandbox modes and approval policies a client may ask for) and managed
# config (the tool keys kept out of commands), read-only, outside /config and every sandbox's writable roots.
COPY gate/requirements.toml gate/managed_config.toml /etc/codex/
# The shipped requirements, from which full_access.py writes /etc/codex/requirements.toml at start.
COPY gate/requirements.toml /usr/share/ha_codex/requirements.toml
RUN chmod 0644 /etc/codex/requirements.toml /etc/codex/managed_config.toml /usr/share/ha_codex/requirements.toml \
    && chown -R root:root /etc/codex /usr/share/ha_codex

# The GitHub sign-in (CX-GITHUB): the worker side, the stdio tool Codex calls, git's askpass helper, and the
# kit's push guard, read-only in /usr/share/ha_codex. Every push the worker makes for Codex runs that guard first.
COPY github_access.py github_mcp.py /
COPY github_askpass.py /usr/local/bin/codex-github-askpass
COPY push_guard.py /usr/share/ha_codex/push_guard.py
RUN chmod 0555 /usr/local/bin/codex-github-askpass && chmod 0444 /usr/share/ha_codex/push_guard.py \
    && chown root:root /usr/local/bin/codex-github-askpass /usr/share/ha_codex/push_guard.py

COPY full_access.py core_address.py hass_mcp.py relay_reads.py relay_reads_mcp.py scheduling.py retention.py schedule_api.py scheduling_mcp.py approvals.py policy.py remote_control.py relay_diagnostics.py verification.py verification_mcp.py ha_data.py ha_data_mcp.py ha_call.py ha_call_mcp.py name_match.py tool_threads.py /
COPY web /web
# The Codex integration of the same release, which integration_sync.py installs in /config at start.
# The build copies custom_components/codex_cli into integration/ first (see integration/README.md).
COPY integration /usr/share/ha_codex/integration
COPY integration_sync.py /
# The protected permission policy: read-only, outside /config and every sandbox's writable roots.
COPY policy/policy.json /usr/share/ha_codex/policy.json
RUN chmod 0444 /usr/share/ha_codex/policy.json

RUN chmod a+x /run.sh /server.py

# Bundle spreadsheet support in the system Python used by Codex's sandbox.
# Both MIT-licensed wheels are pure Python and work on either image architecture.
RUN printf '%s\n' \
        'openpyxl==3.1.5 --hash=sha256:5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2' \
        'et-xmlfile==2.0.0 --hash=sha256:7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa' \
    | python3 -m pip install --break-system-packages --no-cache-dir --no-compile \
        --only-binary=:all: --require-hashes -r /dev/stdin

CMD ["/run.sh"]
