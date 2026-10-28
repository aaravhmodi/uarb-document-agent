# The agent and its browser in one container: Playwright drives a real (headed) Chromium on a
# virtual screen, which you can watch over noVNC on port 6080.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv xvfb x11vnc novnc websockify openbox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY uarb_agent ./uarb_agent
# A venv: the image's Debian-managed packages can't be upgraded by pip. Same Playwright
# version as the image, so it uses the browsers already in it.
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir . "playwright==1.64.1"
ENV PATH="/venv/bin:$PATH"

COPY start.sh /start.sh
CMD ["/bin/sh", "/start.sh"]
