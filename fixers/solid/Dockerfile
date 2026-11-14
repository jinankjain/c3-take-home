# docker build --platform linux/amd64 -t c3-solid-fixer fixers/solid
FROM --platform=linux/amd64 python:3.12.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends grep && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pyyaml pytest
WORKDIR /fixer
COPY fixer /fixer/fixer
COPY run.sh /fixer/run.sh
ENTRYPOINT ["/bin/sh", "/fixer/run.sh"]
