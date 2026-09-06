#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/scripts/phone_agent.py" install "$@"
