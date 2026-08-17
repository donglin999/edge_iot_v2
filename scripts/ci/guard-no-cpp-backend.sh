#!/usr/bin/env bash
# Reject a tracked C++ backend or a runtime/build entry point that restores it.
# Local untracked experiments are outside this policy and are ignored by git.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="${1:-$DEFAULT_REPO_ROOT}"

if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "no-cpp-backend guard: not a Git repository: $REPO_ROOT" >&2
  exit 2
fi

TRACKED_LIST="$(mktemp "${TMPDIR:-/tmp}/edge-iot-no-cpp-tracked.XXXXXX")"
VIOLATIONS="$(mktemp "${TMPDIR:-/tmp}/edge-iot-no-cpp-violations.XXXXXX")"
trap 'rm -f "$TRACKED_LIST" "$VIOLATIONS"' EXIT

git -C "$REPO_ROOT" ls-files >"$TRACKED_LIST"

is_allowed_reference_file() {
  case "$1" in
    .gitignore|\
    deploy/offline/Dockerfile.backend.dockerignore|\
    deploy/offline/Dockerfile.web.dockerignore|\
    docs/migration/m0-baseline.md|\
    docs/migration/vue3-fastapi-milestones.md|\
    scripts/ci/guard-no-cpp-backend.sh|\
    scripts/ci/test-guard-no-cpp-backend.sh)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_build_or_runtime_file() {
  case "$1" in
    *.sh|*.bash|*.zsh|*.mk|makefile|*/makefile|\
    dockerfile|*/dockerfile|dockerfile.*|*/dockerfile.*|\
    *.yml|*.yaml|*.toml|*.json|*.bzl|\
    cmakelists.txt|*/cmakelists.txt|meson.build|*/meson.build|\
    meson_options.txt|*/meson_options.txt|build|*/build|\
    build.bazel|*/build.bazel|workspace|*/workspace|\
    workspace.bazel|*/workspace.bazel|module.bazel|*/module.bazel)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_cpp_build_entry() {
  case "$1" in
    cmakelists.txt|*/cmakelists.txt|cmakepresets.json|*/cmakepresets.json|\
    conanfile.py|*/conanfile.py|conanfile.txt|*/conanfile.txt|\
    vcpkg.json|*/vcpkg.json|meson.build|*/meson.build|\
    meson_options.txt|*/meson_options.txt|build|*/build|\
    build.bazel|*/build.bazel|workspace|*/workspace|\
    workspace.bazel|*/workspace.bazel|module.bazel|*/module.bazel)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_backend_scope() {
  case "$1" in
    backend|backend/*|backend-*|backend-*/*|backend_*|backend_*/*|\
    */backend|*/backend/*|*/backend-*|*/backend-*/*|\
    */backend_*|*/backend_*/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

while IFS= read -r path; do
  lower_path="$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')"

  case "$lower_path" in
    docs/cpp-migration|docs/cpp-migration/*)
      echo "forbidden tracked retired C++ migration path: $path" >>"$VIOLATIONS"
      ;;
    backend_cpp|backend_cpp/*|*/backend_cpp|*/backend_cpp/*|\
    backend-cpp|backend-cpp/*|*/backend-cpp|*/backend-cpp/*|\
    cpp_backend|cpp_backend/*|*/cpp_backend|*/cpp_backend/*|\
    cpp-backend|cpp-backend/*|*/cpp-backend|*/cpp-backend/*)
      echo "forbidden tracked C++ backend path: $path" >>"$VIOLATIONS"
      ;;
  esac

  # Reject C/C++ implementation only in backend scope. Unrelated repository
  # tooling may legitimately use native helpers; lower-casing still catches
  # backend .CPP/.HPP and mixed-case extensions.
  if is_backend_scope "$lower_path"; then
    case "$lower_path" in
      *.c|*.cc|*.cpp|*.cxx|*.h|*.hh|*.hpp|*.hxx)
        echo "forbidden tracked backend C/C++ source: $path" >>"$VIOLATIONS"
        ;;
    esac
  fi

  # Build-system names alone may belong to unrelated tooling. Reject them when
  # they are in backend scope; source files and backend references are checked
  # independently, so root/frontend/tool CMake files are not false positives.
  if is_cpp_build_entry "$lower_path" && is_backend_scope "$lower_path"; then
    echo "forbidden tracked C++ build entry point: $path" >>"$VIOLATIONS"
  fi

  if is_allowed_reference_file "$path" || ! is_build_or_runtime_file "$lower_path"; then
    continue
  fi

  # CI must be able to invoke this guard by its descriptive filename. Remove
  # only that literal script path before scanning the rest of each line; a
  # second forbidden reference on the same line must still fail the gate.
  if git -C "$REPO_ROOT" show ":$path" 2>/dev/null | awk '
      {
        line = tolower($0)
        gsub(/scripts\/ci\/(test-)?guard-no-cpp-backend\.sh/, "", line)
        if (line ~ /(backend[_-]cpp|cpp[_-]backend|edge[_-]iot[_-]cpp)/) {
          found = 1
        }
      }
      END { exit found ? 0 : 1 }
    '; then
    echo "forbidden C++ backend reference in build/runtime file: $path" >>"$VIOLATIONS"
  fi
done <"$TRACKED_LIST"

if [ -s "$VIOLATIONS" ]; then
  echo "C++ backend exclusion gate failed:" >&2
  sed 's/^/  - /' "$VIOLATIONS" >&2
  echo "Only the two Docker ignore files and the migration exclusion policy may retain references." >&2
  exit 1
fi

echo "C++ backend exclusion gate passed ($(wc -l <"$TRACKED_LIST" | tr -d ' ') tracked files checked)."
