#!/usr/bin/env bash
# Static gate: production-like credentials and insecure fallbacks must not return.
set -euo pipefail
cd "$(dirname "$0")/../.."

fail() {
  echo "offline secret safety gate: $1" >&2
  exit 1
}

COMPOSE=deploy/offline/docker-compose.offline.yml
EXAMPLE=deploy/offline/.env.example
LOADER=scripts/offline/load-and-up.sh

# The distributed example is intentionally incomplete and must never be runnable as-is.
awk -F= '
  /^(SECRET_KEY|INFLUXDB_TOKEN|ALLOWED_HOSTS|INFLUXDB_ORG|INFLUXDB_BUCKET)=/ {
    if (length(substr($0, index($0, "=") + 1)) != 0) {
      print FNR ":" $0
      bad = 1
    }
  }
  END { exit bad }
' "$EXAMPLE" || fail "$EXAMPLE contains a credential or site-specific production value"

grep -Fq 'DEBUG=False' "$EXAMPLE" || fail "example must default DEBUG to False"
grep -Fq 'ALLOW_PRIVATE_NETWORK_TESTS=False' "$EXAMPLE" \
  || fail "example must keep private-network connection tests disabled"

if grep -nE '\$\{(SECRET_KEY|INFLUXDB_TOKEN|ALLOWED_HOSTS|INFLUXDB_ORG|INFLUXDB_BUCKET):-' "$COMPOSE"; then
  fail "required production values must not have Compose fallbacks"
fi
for required in SECRET_KEY INFLUXDB_TOKEN ALLOWED_HOSTS INFLUXDB_ORG INFLUXDB_BUCKET; do
  grep -Fq '${'"$required"':?' "$COMPOSE" \
    || fail "$required is not fail-closed in Compose"
done
if grep -nE '(DEBUG|ALLOW_PRIVATE_NETWORK_TESTS)=\$\{[^}]*:-([Tt]rue|1|[Yy]es|[Oo]n)' "$COMPOSE"; then
  fail "Compose contains an insecure boolean fallback"
fi
grep -Fq 'redis-server --bind 127.0.0.1 --protected-mode yes' "$COMPOSE" \
  || fail "bundled Redis must remain loopback-only and protected"

# Catch long token/password/secret-looking assignment literals in tracked runtime
# code and deployment docs. Test fixtures are excluded because they intentionally
# use synthetic credentials to exercise redaction and authentication paths.
python3 - <<'PY'
import pathlib
import re
import subprocess
import sys

assignment = re.compile(
    r"(?i)[\"']?(?P<key>[A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET_KEY|SECRET))[\"']?"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{32,})"
)
bad = []
tracked = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
for name in tracked:
    path = pathlib.Path(name)
    if not name or "tests" in path.parts or path.name.startswith(("test_", "test-")):
        continue
    if name.endswith(("package-lock.json", ".lock", ".png", ".jpg", ".woff", ".woff2")):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for line_number, line in enumerate(text.splitlines(), 1):
        if assignment.search(line):
            bad.append(f"{path}:{line_number}: credential-shaped assignment")
if bad:
    print("\n".join(bad), file=sys.stderr)
    raise SystemExit(1)
PY

validate_line=$(grep -nE '^[[:space:]]*\./validate-env\.sh ' "$LOADER" | head -1 | cut -d: -f1)
load_line=$(grep -n '^docker load ' "$LOADER" | head -1 | cut -d: -f1)
[ -n "$validate_line" ] && [ -n "$load_line" ] && [ "$validate_line" -lt "$load_line" ] \
  || fail "loader must validate secrets before docker load or runtime mutation"
grep -Fq 'validate-env.sh' scripts/offline/build-offline-bundle.sh \
  || fail "offline bundle must include validate-env.sh"

echo "offline secret safety gate: OK"
