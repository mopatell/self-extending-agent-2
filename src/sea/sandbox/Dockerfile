# Image every agent-written tool runs in. Build with `make sandbox`.
FROM python:3.12-slim

# The allowlist. Keep in sync with sea/sandbox/allowlist.py.
RUN pip install --no-cache-dir \
    requests==2.32.3 \
    beautifulsoup4==4.12.3 \
    pandas==2.2.3 \
    pypdf==5.1.0 \
    pyyaml==6.0.2 \
    python-dateutil==2.9.0.post0

RUN useradd --create-home --uid 1000 runner
USER runner
WORKDIR /workspace
