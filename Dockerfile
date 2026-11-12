FROM debian:forky-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PORT=6020

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libvips-tools \
        libheif-plugin-libde265 \
        imagemagick \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY server.py /server.py

RUN python3 /server.py --check \
    && python3 /server.py --self-test

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin thumbnail
USER thumbnail

EXPOSE 6020

CMD ["python3", "/server.py"]
