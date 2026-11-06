FROM --platform=linux/amd64 python:3.12.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /fixer
COPY requirements.txt /tmp/fixer-requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/fixer-requirements.txt \
    && rm /tmp/fixer-requirements.txt

COPY fixer/ /fixer/fixer/
COPY run.sh /fixer/run.sh
RUN chmod 0755 /fixer/run.sh

ENTRYPOINT ["/bin/sh", "/fixer/run.sh"]
