#!/usr/bin/env python3
"""Validate the exact numeric fact restored into the disposable Influx bucket."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional, Sequence


MAX_CSV_BYTES = 1024 * 1024


class ValueCheckError(RuntimeError):
    """The restored query result does not prove the expected fact."""


def _read_regular_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueCheckError(f"could not securely open query result: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or before.st_size > MAX_CSV_BYTES
        ):
            raise ValueCheckError("query result is not one bounded regular file")
        chunks = []
        remaining = MAX_CSV_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise ValueCheckError("query result exceeds the size limit")
        after = os.fstat(descriptor)
        current_after = os.lstat(path)
        if (
            (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (current_after.st_dev, current_after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise ValueCheckError("query result changed while it was read")
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueCheckError("query result is not UTF-8 CSV") from exc


def parse_numeric_values(raw_csv: str) -> tuple[Decimal, ...]:
    values = []
    value_index: Optional[int] = None
    for row in csv.reader(io.StringIO(raw_csv)):
        if not row or row[0].startswith("#"):
            continue
        if "_value" in row:
            value_index = row.index("_value")
            continue
        if value_index is None:
            continue
        if value_index >= len(row) or not row[value_index].strip():
            continue
        try:
            value = Decimal(row[value_index].strip())
        except InvalidOperation as exc:
            raise ValueCheckError("query result contains a non-numeric _value") from exc
        if not value.is_finite():
            raise ValueCheckError("query result contains a non-finite _value")
        values.append(value)
    return tuple(values)


def verify_known_value(path_value: str, expected_value: str) -> dict[str, object]:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueCheckError("query result path must be absolute")
    try:
        expected = Decimal(expected_value)
    except InvalidOperation as exc:
        raise ValueCheckError("expected value must be numeric") from exc
    if not expected.is_finite():
        raise ValueCheckError("expected value must be finite")
    values = parse_numeric_values(_read_regular_file(path))
    if values != (expected,):
        raise ValueCheckError(
            "restored query must contain exactly one row equal to the expected value "
            f"(numeric rows observed: {len(values)})"
        )
    return {
        "operation": "restored-influx-known-value-check",
        "expected_value": str(expected),
        "numeric_row_count": 1,
        "result": "ok",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--expected", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_known_value(args.path, args.expected)
    except (OSError, ValueCheckError) as exc:
        print(f"VALUE CHECK ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
