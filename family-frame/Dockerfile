ARG BUILD_FROM=ghcr.io/home-assistant/base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

COPY sync.py index.html /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
