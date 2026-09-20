# Linux runner images

These Dockerfiles were imported from ACE-AI `/opt/ci-runners` so architecture
changes can be reviewed with the workflows that consume them. The heavy base
is unchanged; build it natively on each architecture before the derived image.
No runner registration, pool configuration, or live image tag is changed by this build.

From the repository root, use a small build context:

```sh
context=$(mktemp -d)
cp .github/runner/Dockerfile.* "$context/"
cp .github/scripts/install-ci-tool.sh "$context/"
docker build -f "$context/Dockerfile.heavy" -t ci-runner-heavy:node20 "$context"
docker build -f "$context/Dockerfile.ang-hermes" -t ci-runner-ang-hermes:arch-candidate "$context"
```

Run this on both Linux x86_64 and aarch64 (or a native Linux container VM).
The shared installer selects by `uname -m`, rejects unsupported architectures,
checks the pinned SHA256 before extraction/installation, and installs job-time
binaries under `~/.local/bin` without sudo. Ripgrep's arm64 release uses GNU
libc; these Ubuntu Noble runners provide it. Hashes come from the respective
15.1.0 ripgrep `.sha256`, 8.18.4 gitleaks `checksums.txt`, and 2.12.0 hadolint
`.sha256` release assets. Update each architecture's hash when updating a tool.

Verification must execute the workflow installer for ripgrep and gitleaks and
check their versions. Also inspect the ELF `e_machine` field (183 for AArch64,
62 for x86-64): Docker Desktop can transparently emulate a wrong-architecture
binary, so `--version` alone does not prove portability. Then install the exact
`tests.yml` locked uv extras and run generated slice 1/16 with
`scripts/run_tests.sh --files`, on both architectures. Offline installer gates:
`python -m pytest tests/test_ci_tool_install.py -q`.

After review, deploy the Dockerfiles AND the matching installer into
`/opt/ci-runners` together before building. Keep the candidate tag separate from
`ci-runner-ang-hermes:1` until release approval. Do not stop active job containers;
a pool rollout must drain/replace idle runners only. Adding an arm64 host to the
pool is a separate operation after these workflow changes land.
