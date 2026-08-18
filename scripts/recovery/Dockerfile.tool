FROM python:3.10-slim AS influx-cli

ARG TARGETARCH
ARG INFLUX_CLI_VERSION=2.7.5
ARG INFLUX_CLI_SHA256=496dffcd70bed2bb3dc3d614e3d9c97e312e092dfe0577d332027566bbb7d8cd

RUN test "$TARGETARCH" = "amd64" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && archive="/tmp/influxdb2-client-${INFLUX_CLI_VERSION}-linux-amd64.tar.gz" \
    && curl --fail --show-error --silent --location \
        "https://dl.influxdata.com/influxdb/releases/influxdb2-client-${INFLUX_CLI_VERSION}-linux-amd64.tar.gz" \
        --output "$archive" \
    && printf '%s  %s\n' "$INFLUX_CLI_SHA256" "$archive" | sha256sum --check --strict - \
    && tar -xzf "$archive" -C /tmp ./influx \
    && install -m 0755 /tmp/influx /usr/local/bin/influx \
    && /usr/local/bin/influx version \
    && rm -f "$archive" /tmp/influx

FROM python:3.10-slim
COPY --from=influx-cli /usr/local/bin/influx /usr/local/bin/influx
WORKDIR /repo
COPY scripts/backup/ /repo/scripts/backup/
ENTRYPOINT []
CMD ["python", "--version"]
