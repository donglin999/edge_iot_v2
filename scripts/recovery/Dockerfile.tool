FROM influxdb:2.7 AS influx

FROM python:3.10-slim
COPY --from=influx /usr/bin/influx /usr/local/bin/influx
WORKDIR /repo
COPY scripts/backup/ /repo/scripts/backup/
ENTRYPOINT []
CMD ["python", "--version"]
