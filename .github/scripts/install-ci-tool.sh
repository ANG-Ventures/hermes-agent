#!/usr/bin/env bash
# Pinned Linux release binaries for jobs and runner-image builds.
set -euo pipefail

tool=${1:?Usage: install-ci-tool.sh ripgrep|gitleaks|hadolint [destination]}
destination=${2:-"$HOME/.local/bin"}
arch=$(uname -m)
case "$tool:$arch" in
  ripgrep:x86_64)
    asset=ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz
    sha=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599 ;;
  ripgrep:aarch64)
    asset=ripgrep-15.1.0-aarch64-unknown-linux-gnu.tar.gz
    sha=2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e ;;
  gitleaks:x86_64)
    asset=gitleaks_8.18.4_linux_x64.tar.gz
    sha=ba6dbb656933921c775ee5a2d1c13a91046e7952e9d919f9bac4cec61d628e7d ;;
  gitleaks:aarch64)
    asset=gitleaks_8.18.4_linux_arm64.tar.gz
    sha=bf5f7f466ebfade1296c8bd32cf7d3f592c2aa78836aa9980ffbe2cadca7a861 ;;
  hadolint:x86_64)
    asset=hadolint-Linux-x86_64
    sha=56de6d5e5ec427e17b74fa48d51271c7fc0d61244bf5c90e828aab8362d55010 ;;
  hadolint:aarch64)
    asset=hadolint-Linux-arm64
    sha=5798551bf19f33951881f15eb238f90aef023f11e7ec7e9f4c37961cb87c5df6 ;;
  *) printf 'Unsupported CI tool/architecture: %s:%s\n' "$tool" "$arch" >&2; exit 1 ;;
esac
case "$tool" in
  ripgrep) release=BurntSushi/ripgrep/releases/download/15.1.0; binary=rg ;;
  gitleaks) release=gitleaks/gitleaks/releases/download/v8.18.4; binary=gitleaks ;;
  hadolint) release=hadolint/hadolint/releases/download/v2.12.0; binary=hadolint ;;
esac
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
curl -fsSL --retry 3 --retry-delay 5 -o "$scratch/$asset" "https://github.com/$release/$asset"
printf '%s  %s\n' "$sha" "$scratch/$asset" | sha256sum -c -
case "$tool" in
  ripgrep) tar -xzf "$scratch/$asset" -C "$scratch" --strip-components=1 "${asset%.tar.gz}/rg" ;;
  gitleaks) tar -xzf "$scratch/$asset" -C "$scratch" gitleaks ;;
  hadolint) mv "$scratch/$asset" "$scratch/hadolint" ;;
esac
mkdir -p "$destination"
install -m 0755 "$scratch/$binary" "$destination/$binary"
if [[ "$tool" == gitleaks ]]; then
  "$destination/$binary" version
else
  "$destination/$binary" --version
fi
if [[ -n "${GITHUB_PATH:-}" ]]; then
  printf '%s\n' "$destination" >> "$GITHUB_PATH"
fi
