#!/usr/bin/env bash
# Hermetic self-test for guard-no-cpp-backend.sh.  It uses only temporary Git
# indexes, so the project worktree is never changed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$SCRIPT_DIR/guard-no-cpp-backend.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/edge-iot-no-cpp-test.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

new_repo() {
  local name="$1"
  local repo="$TMP_ROOT/$name"
  mkdir -p "$repo"
  git -C "$repo" init -q
  echo "$repo"
}

expect_pass() {
  local repo="$1"
  if ! output="$(bash "$GUARD" "$repo" 2>&1)"; then
    echo "expected guard success for $repo" >&2
    echo "$output" >&2
    exit 1
  fi
}

expect_fail_with() {
  local repo="$1"
  local expected="$2"
  if output="$(bash "$GUARD" "$repo" 2>&1)"; then
    echo "expected guard failure for $repo" >&2
    exit 1
  fi
  case "$output" in
    *"$expected"*) ;;
    *)
      echo "guard failed without expected diagnostic '$expected'" >&2
      echo "$output" >&2
      exit 1
      ;;
  esac
}

repo="$(new_repo allowed-policy)"
mkdir -p \
  "$repo/deploy/offline" \
  "$repo/docs/migration" \
  "$repo/tools/demo" \
  "$repo/frontend" \
  "$repo/.github/workflows"
printf 'backend_cpp\n' >"$repo/deploy/offline/Dockerfile.backend.dockerignore"
printf 'backend_cpp\n' >"$repo/deploy/offline/Dockerfile.web.dockerignore"
printf 'backend_cpp is retired\n' >"$repo/docs/migration/m0-baseline.md"
printf '/backend_cpp/\n' >"$repo/.gitignore"
printf 'project(unrelated_tool)\n' >"$repo/tools/demo/CMakeLists.txt"
printf 'project("unrelated-tool")\n' >"$repo/tools/demo/meson.build"
printf 'int helper() { return 0; }\n' >"$repo/tools/demo/helper.cpp"
printf '# unrelated frontend build metadata\n' >"$repo/frontend/BUILD.bazel"
printf 'run: bash scripts/ci/guard-no-cpp-backend.sh\n' >"$repo/.github/workflows/ci.yml"
git -C "$repo" add .
expect_pass "$repo"

repo="$(new_repo forbidden-directory)"
mkdir -p "$repo/backend_cpp"
printf 'int main() { return 0; }\n' >"$repo/backend_cpp/main.cpp"
git -C "$repo" add .
expect_fail_with "$repo" "forbidden tracked C++ backend path"

repo="$(new_repo forbidden-source-case-insensitive)"
mkdir -p "$repo/backend/native"
printf 'int main() { return 0; }\n' >"$repo/backend/native/server.CPP"
git -C "$repo" add .
expect_fail_with "$repo" "forbidden tracked backend C/C++ source"

repo="$(new_repo forbidden-retired-docs)"
mkdir -p "$repo/docs/cpp-migration"
printf '/docs/cpp-migration/\n' >"$repo/.gitignore"
printf '# retired implementation notes\n' >"$repo/docs/cpp-migration/README.md"
git -C "$repo" add .gitignore
git -C "$repo" add -f docs/cpp-migration/README.md
expect_fail_with "$repo" "forbidden tracked retired C++ migration path"

repo="$(new_repo forbidden-meson)"
mkdir -p "$repo/backend/native"
printf 'project("edge")\n' >"$repo/backend/native/meson.build"
git -C "$repo" add .
expect_fail_with "$repo" "forbidden tracked C++ build entry point"

repo="$(new_repo forbidden-bazel)"
mkdir -p "$repo/backend"
printf '# generated sources would be attached here\n' >"$repo/backend/BUILD.bazel"
git -C "$repo" add .
expect_fail_with "$repo" "forbidden tracked C++ build entry point"

repo="$(new_repo forbidden-compose-reference)"
printf 'services:\n  backend:\n    image: edge-iot-cpp:latest\n' >"$repo/docker-compose.yml"
git -C "$repo" add .
expect_fail_with "$repo" "forbidden C++ backend reference"

repo="$(new_repo forbidden-ci-reference-after-guard-call)"
mkdir -p "$repo/.github/workflows"
printf '%s\n' \
  'run: bash scripts/ci/guard-no-cpp-backend.sh && docker run edge-iot-cpp:latest' \
  >"$repo/.github/workflows/ci.yml"
git -C "$repo" add .
expect_fail_with "$repo" "forbidden C++ backend reference"

echo "guard-no-cpp-backend self-test passed."
