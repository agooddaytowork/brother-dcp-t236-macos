#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != Darwin ]; then
  echo 'This driver requires macOS.' >&2; exit 1
fi
if [ "$(id -u)" = 0 ]; then
  echo 'Run as your normal Mac user. Do not run the whole installer with sudo.' >&2; exit 1
fi
major="$(sw_vers -productVersion | cut -d. -f1)"
if [ "$major" -lt 14 ]; then
  echo 'macOS 14 or later is required. Hardware testing was on macOS 26 / Apple Silicon.' >&2; exit 1
fi
if ! command -v brew >/dev/null 2>&1; then
  if [ -x /opt/homebrew/bin/brew ]; then
    export PATH="/opt/homebrew/bin:$PATH"
  elif [ -x /usr/local/bin/brew ]; then
    export PATH="/usr/local/bin:$PATH"
  else
    echo 'Install Homebrew from https://brew.sh, then run this installer again.' >&2; exit 1
  fi
fi
brew list --versions python@3.14 >/dev/null || brew install python@3.14
brew list --versions libusb >/dev/null || brew install libusb
py="$(brew --prefix python@3.14)/bin/python3.14"
if [ ! -f bin/brother-t236-print ]; then
  if ! xcrun --find clang >/dev/null 2>&1; then
    echo 'Source installation needs Apple Command Line Tools. Run: xcode-select --install' >&2; exit 1
  fi
  make all
fi
exec "$py" scripts/manage.py install "$@"
