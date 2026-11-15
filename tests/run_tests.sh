#!/bin/bash
# Run MintWatch tests. Unit tests always run offline.
# Pass --live to also run the live tests (public BSC RPC + local HTTP server).
set -e
cd "$(dirname "$0")/.."
if [ "$1" = "--live" ]; then
  MINTWATCH_LIVE=1 python3 -m unittest discover -s tests -v
else
  python3 -m unittest discover -s tests -v
fi