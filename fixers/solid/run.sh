#!/bin/sh
# Entry point: the case workspace is $C3_WORKSPACE (also the working directory).
cd "$(dirname "$0")" && FIXER_DIR="$(pwd)" && cd "${C3_WORKSPACE:-/workspace}" && \
  PYTHONPATH="$FIXER_DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m fixer.main "$@"
