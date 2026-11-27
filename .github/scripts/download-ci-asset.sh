#!/usr/bin/env bash
# Download a pinned CI asset. Never consume bytes before verifying the caller's hash.
set -euo pipefail

url=${1:?URL required}
api_url=${2:?API asset URL required}
sha=${3:?SHA-256 required}
output=${4:?Output path required}
# Do not leave an unverified candidate for a later consumer after any failure.
trap 'rm -f "$output"' EXIT
attempts=6
for ((n = 1; ; n++)); do
  headers=()
  if ((n > 1)); then
    url=$api_url
    headers=(-H 'Accept: application/octet-stream')
  fi
  # Public endpoints need no token. -q ignores curlrc (credentials, proxies,
  # location-trusted); curl's normal redirect policy never forwards credentials.
  if curl -q -fsSL --proto '=https' --proto-redir '=https' --max-redirs 5 \
      --connect-timeout 20 --max-time 120 ${headers[@]+"${headers[@]}"} -o "$output" "$url"; then
    printf '%s  %s\n' "$sha" "$output" | sha256sum -c -
    trap - EXIT
    break
  fi
  if ((n >= attempts)); then
    printf 'download failed after %s attempts: %s\n' "$attempts" "$url" >&2
    exit 1
  fi
  printf 'download attempt %s/%s failed; retrying in %ss\n' "$n" "$attempts" "$((n * 5))" >&2
  sleep "$((n * 5))"
done
