#!/usr/bin/env bash
# Static contract for the shared Python 3.10 constraints and every installer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONSTRAINTS="$REPO_ROOT/backend/constraints-py310.txt"

case "${1:-}" in
  "") CHECK_INSTALLED=false ;;
  --installed) CHECK_INSTALLED=true ;;
  *)
    echo "usage: $0 [--installed]" >&2
    exit 2
    ;;
esac
[ "$#" -le 1 ] || { echo "usage: $0 [--installed]" >&2; exit 2; }

fail() {
  echo "python constraints check failed: $*" >&2
  exit 1
}

[ -f "$CONSTRAINTS" ] || fail "missing backend/constraints-py310.txt"

if ! awk '
  /^[[:space:]]*($|#)/ { next }
  $0 !~ /^[A-Za-z0-9_.-]+==[^[:space:]]+$/ {
    print "non-exact constraint at line " NR ": " $0 > "/dev/stderr"
    bad = 1
  }
  {
    split($0, fields, "==")
    name = tolower(fields[1])
    gsub(/[-_.]+/, "-", name)
    if (seen[name]++) {
      print "duplicate constraint at line " NR ": " fields[1] > "/dev/stderr"
      bad = 1
    }
  }
  END { exit bad }
' "$CONSTRAINTS"; then
  fail "constraints must contain one exact pin per package"
fi

if ! awk '
  function canonical(name) {
    name = tolower(name)
    gsub(/[-_.]+/, "-", name)
    return name
  }
  NR == FNR {
    if ($0 ~ /^[[:space:]]*($|#)/) next
    split($0, fields, "==")
    pinned[canonical(fields[1])] = 1
    next
  }
  /^[[:space:]]*($|#|-)/ { next }
  {
    line = $0
    sub(/[[:space:]]+#.*/, "", line)
    name = line
    sub(/\[.*/, "", name)
    sub(/[<>=!~].*/, "", name)
    gsub(/[[:space:]]/, "", name)
    if (!pinned[canonical(name)]) {
      print "unconstrained requirement in " FILENAME ": " name > "/dev/stderr"
      bad = 1
    }
  }
  END { exit bad }
' "$CONSTRAINTS" \
    "$REPO_ROOT/backend/requirements.txt" \
    "$REPO_ROOT/backend/requirements-websocket.txt" \
    "$REPO_ROOT/backend/tests/requirements-test.txt"; then
  fail "every declared runtime/test dependency must have an exact constraint"
fi

for pin in \
  'Twisted==26.4.0' \
  'pyOpenSSL==25.3.0' \
  'cryptography==45.0.7' \
  'async-timeout==5.0.1' \
  'tomli==2.4.1' \
  'setuptools==79.0.1' \
  'celery==5.4.0' \
  'kombu==5.4.2' \
  'amqp==5.3.1'; do
  grep -Fqx "$pin" "$CONSTRAINTS" || fail "missing resolver anchor: $pin"
done

for consumer in \
  backend/Dockerfile \
  deploy/offline/Dockerfile.backend \
  scripts/offline/build-wheelhouse.sh \
  backend/run_tests.sh \
  backend/requirements-websocket.txt \
  docs/QUICKSTART.md \
  backend/tests/README.md \
  .github/workflows/ci.yml; do
  grep -Fq 'constraints-py310.txt' "$REPO_ROOT/$consumer" \
    || fail "$consumer does not consume constraints-py310.txt"
done

grep -Fq 'pip check' "$REPO_ROOT/backend/Dockerfile" \
  || fail "development Docker image must run pip check"
grep -Fq 'pip check' "$REPO_ROOT/deploy/offline/Dockerfile.backend" \
  || fail "offline Docker image must run pip check"
grep -Fq 'pip check' "$REPO_ROOT/.github/workflows/ci.yml" \
  || fail "CI must run pip check"

grep -Fq 'cpython 3.10.' "$REPO_ROOT/scripts/offline/build-wheelhouse.sh" \
  || fail "wheelhouse builder must reject non-CPython-3.10 resolvers"
grep -Fq -- '--check-python' "$REPO_ROOT/scripts/offline/build-wheelhouse.sh" \
  || fail "wheelhouse builder must expose a non-network resolver preflight"
grep -Fq 'test-wheelhouse-python.sh' "$REPO_ROOT/.github/workflows/ci.yml" \
  || fail "CI must run the wheelhouse resolver self-test"
grep -Fq 'check-python-constraints.sh --installed' "$REPO_ROOT/.github/workflows/ci.yml" \
  || fail "CI must verify the installed dependency closure against constraints"

echo "Python 3.10 constraints contract passed."

if [ "$CHECK_INSTALLED" = true ]; then
  python - "$CONSTRAINTS" <<'PY'
import importlib.metadata
import re
import sys


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


constraints = {}
with open(sys.argv[1], encoding="utf-8") as stream:
    for raw_line in stream:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        constraints[canonical(name)] = version

# pip is the resolver itself and is pinned by the CI install command; wheel is
# setup-python tooling rather than an application dependency.
ignored_tooling = {"pip", "wheel"}
problems = []
checked = 0
for distribution in importlib.metadata.distributions():
    raw_name = distribution.metadata.get("Name")
    if not raw_name:
        continue
    name = canonical(raw_name)
    if name in ignored_tooling:
        continue
    checked += 1
    expected = constraints.get(name)
    if expected is None:
        problems.append(f"unconstrained installed distribution: {raw_name}=={distribution.version}")
    elif distribution.version != expected:
        problems.append(
            f"installed version drift: {raw_name}=={distribution.version}, expected {expected}"
        )

if problems:
    print("installed Python dependency closure is not reproducible:", file=sys.stderr)
    for problem in sorted(problems):
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)

print(f"Installed dependency closure matches constraints ({checked} distributions checked).")
PY
fi
