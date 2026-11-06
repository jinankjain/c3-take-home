#!/bin/sh
set -eu
C3_FIXER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$C3_FIXER_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "${C3_FIXER_PYTHON:-python3}" -m fixer.main "$@"
