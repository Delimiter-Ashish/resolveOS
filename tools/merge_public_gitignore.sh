#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
marker='# --- ResolveOS public release:'
if grep -Fq "$marker" .gitignore 2>/dev/null; then
  echo 'Public ignore rules already present. No changes made.'
else
  cat PUBLIC_GITIGNORE.snippet >> .gitignore
  echo 'Appended safe public ignore rules to existing .gitignore.'
fi
