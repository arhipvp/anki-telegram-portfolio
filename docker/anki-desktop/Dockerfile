FROM lsiobase/kasmvnc:ubuntunoble@sha256:139962365ba29ae51727ae5d979b1a58ecf497463c6ffd5db24fa5a9e35d656e

ARG ANKI_VERSION=25.07.5
ARG ANKICONNECT_VERSION=23.10.29.0

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        anki \
        espeak-ng \
        libxcb-cursor0 \
        libxcb-xinerama0 \
        patch \
        python3-xdg \
        wget \
        xdg-utils \
        zstd \
    && dpkg --remove anki \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install --no-install-recommends -y libatomic1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q "https://github.com/ankitects/anki/releases/download/${ANKI_VERSION}/anki-launcher-${ANKI_VERSION}-linux.tar.zst" \
    && tar --use-compress-program=unzstd -xf "anki-launcher-${ANKI_VERSION}-linux.tar.zst" \
    && cd "anki-launcher-${ANKI_VERSION}-linux" \
    && ./install.sh \
    && cd / \
    && rm -rf "/anki-launcher-${ANKI_VERSION}-linux" "/anki-launcher-${ANKI_VERSION}-linux.tar.zst"

RUN mkdir -p /opt/ankiconnect \
    && wget -qO- "https://api.github.com/repos/FooSoft/anki-connect/tarball/${ANKICONNECT_VERSION}" \
       | tar -xz --strip-components=2 -C /opt/ankiconnect --wildcards "*/plugin/*"

COPY anki-connect-auth-status.patch /tmp/anki-connect-auth-status.patch
COPY anki-connect-sync-and-wait.patch /tmp/anki-connect-sync-and-wait.patch
RUN cd /opt/ankiconnect \
    && patch -p1 < /tmp/anki-connect-auth-status.patch \
    && patch -p1 < /tmp/anki-connect-sync-and-wait.patch \
    && rm /tmp/anki-connect-auth-status.patch /tmp/anki-connect-sync-and-wait.patch

COPY root /

EXPOSE 3000 8765
