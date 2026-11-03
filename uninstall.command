#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x '/opt/homebrew/opt/python@3.14/bin/python3.14' ]; then
  py='/opt/homebrew/opt/python@3.14/bin/python3.14'
elif [ -x '/usr/local/opt/python@3.14/bin/python3.14' ]; then
  py='/usr/local/opt/python@3.14/bin/python3.14'
else
  py=python3
fi
exec "$py" scripts/manage.py uninstall "$@"
