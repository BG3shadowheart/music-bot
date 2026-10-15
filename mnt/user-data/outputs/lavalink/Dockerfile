FROM ghcr.io/lavalink-devs/lavalink:4.2.2

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
    && python3 -m pip install --no-cache-dir --break-system-packages --upgrade yt-dlp \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=lavalink:lavalink application.yml /opt/Lavalink/application.yml

USER lavalink
