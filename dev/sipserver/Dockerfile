FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends kamailio netbase && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /var/run/kamailio
EXPOSE 5060/udp 8080/tcp
ENTRYPOINT ["kamailio", "-DD", "-E", "-f", "/etc/kamailio/kamailio.cfg"]
