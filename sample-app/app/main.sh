#!/bin/sh
set -eu

echo "Hello from Docksmith sample app!"
echo "Working directory: $(pwd)"
echo "MESSAGE=$MESSAGE"
echo "Hostname: $(hostname 2>/dev/null || echo unknown)"

if [ -f /app/build.txt ]; then
  echo "Build artifact: $(cat /app/build.txt)"
else
  echo "Build artifact: (missing)"
fi
