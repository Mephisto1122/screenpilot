FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 PYTHONUNBUFFERED=1 DATA_DIR=/data OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pil \
    xvfb xdotool xclip openbox chromium xterm fonts-dejavu fonts-noto-core \
    dbus-x11 x11-utils ca-certificates tini tzdata \
    tint2 mousepad pcmanfm adwaita-icon-theme \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb python3-opencv \
    xdg-utils file shared-mime-info ristretto xarchiver unzip zip p7zip-full \
    curl wget less nano python3-pip python3-venv \
    sudo libdbus-glib-1-2 libgtk-3-0 libasound2 libxtst6 libx11-xcb1 libpci3 xz-utils bzip2 \
    && rm -rf /var/lib/apt/lists/*
# Office apps make the desktop useful for real work (documents, spreadsheets, presentations). Skip them
# for a smaller image with: docker compose build --build-arg OFFICE=0
ARG OFFICE=1
RUN if [ "$OFFICE" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 && rm -rf /var/lib/apt/lists/*; fi
# Screenpilot runs as root inside the container, so it can install apps the agent asks for.
# The agent's desktop (browser, apps, commands) runs as `desk`, which cannot read Screenpilot's
# settings, saved passwords or API keys.
RUN useradd -m -u 1001 -s /bin/bash desk && mkdir -p /data /opt/screenpilot /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
WORKDIR /opt/screenpilot
COPY app ./app
# The agent's terminal may install packages (and only that) as root: sudo sp-install <packages>
RUN install -m 0755 app/desktop/sp-install /usr/local/bin/sp-install && \
    echo 'desk ALL=(root) NOPASSWD: /usr/local/bin/sp-install' > /etc/sudoers.d/screenpilot && chmod 0440 /etc/sudoers.d/screenpilot
COPY start.sh ./start.sh
RUN chmod +x ./start.sh
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./start.sh"]
