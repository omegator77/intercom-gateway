# The SIP bridge as a container (for the server; the gateway and client stay Windows programs).
#   docker build -f Dockerfile.sip -t intercom-sip .
#   docker run --rm --network host -v $PWD/sip.toml:/etc/intercom-sip/sip.toml:ro -e SIP_REGIE_PASSWORD=... intercom-sip
# SIP and RTP are UDP and carry their own addresses in the messages, so the container wants the host's network
# (--network host) or exactly the published SIP and RTP ports with `local_ip` set to the public address.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY gateway ./gateway
COPY intercom_client ./intercom_client
COPY intercom_sip ./intercom_sip
RUN pip install --no-cache-dir .
USER nobody
ENTRYPOINT ["intercom-sip"]
CMD ["run", "-c", "/etc/intercom-sip/sip.toml", "--log-file", ""]
