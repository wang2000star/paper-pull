#!/usr/bin/env python3
"""Read, write, split and concatenate standalone UTF-8-with-BOM year CSVs."""
import argparse
import csv
import filecmp
import io
import re
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path


def csv_paths(source):
    """Accept either a legacy flat file or a directory of year shards."""
    source = Path(source)
    if not source.is_dir():
        return [source]
    paths = sorted(source.glob("*.csv"))
    if not paths:
        raise ValueError(f"No CSV shards in {source}")
    for path in paths:
        if not re.fullmatch(r"(?:[0-9]{4}|_unknown)\.csv", path.name):
            raise ValueError(f"Unexpected shard name: {path}")
    return paths


def read_header(source):
    header = None
    for path in csv_paths(source):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            current = next(csv.reader(stream), None)
        if not current:
            raise ValueError(f"Missing CSV header: {path}")
        if header is not None and current != header:
            raise ValueError(f"CSV header mismatch: {path}")
        header = current
    return header


def iter_rows(source):
    """Yield raw data rows, ignoring blank lines and repeated headers."""
    header = read_header(source)
    for path in csv_paths(source):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            next(reader)
            for row in reader:
                if not row or row == header:
                    continue
                if len(row) != len(header):
                    raise ValueError(f"Wrong column count in {path}, line {reader.line_num}")
                yield row


def iter_dict_rows(source):
    header = read_header(source)
    for row in iter_rows(source):
        yield dict(zip(header, row))


def year_of(row):
    """An explicit year column takes precedence over the ePrint PDF URL."""
    if "year" in row:
        year = str(row["year"] or "").strip()
        return year if re.fullmatch(r"[0-9]{4}", year) else "_unknown"
    match = re.search(r"https?://eprint\.iacr\.org/([0-9]{4})/[0-9]+\.pdf(?:[?#].*)?$",
                      row.get("PDF链接", "") or "")
    return match.group(1) if match else "_unknown"


def write_shards(outdir, header, rows, *, years=None):
    """Replace a dataset, or only selected years, without rewriting equal files.

    All rows are validated and staged before publishing. Scoped writes preserve
    every other year and emit a header-only shard for an empty selected year.
    """
    outdir = Path(outdir)
    allowed = None if years is None else {str(year) for year in years}
    if allowed is not None and any(not re.fullmatch(r"[0-9]{4}", y) for y in allowed):
        raise ValueError("Selected years must have four digits")
    outdir.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with tempfile.TemporaryDirectory(prefix=".shards-", dir=outdir) as staging:
        staging = Path(staging)
        with ExitStack() as stack:
            writers = {}

            def writer_for(year):
                if year not in writers:
                    stream = stack.enter_context((staging / f"{year}.csv").open(
                        "w", encoding="utf-8-sig", newline=""))
                    writers[year] = csv.writer(stream, lineterminator="\n")
                    writers[year].writerow(header)
                    counts[year] = 0
                return writers[year]

            for row in rows:
                values = [row.get(key, "") for key in header] if isinstance(row, dict) else list(row)
                if len(values) != len(header):
                    raise ValueError("Row does not match CSV header")
                year = year_of(dict(zip(header, values)))
                if allowed is not None and year not in allowed:
                    raise ValueError(f"Row year {year} outside selected years {sorted(allowed)}")
                writer_for(year).writerow(values)
                counts[year] += 1
            for year in sorted(allowed or ()):
                writer_for(year)
            if not writers:
                writer_for("_unknown")
        for path in sorted(staging.glob("*.csv")):
            target = outdir / path.name
            if not target.exists() or not filecmp.cmp(path, target, shallow=False):
                path.replace(target)
        for target in outdir.glob("*.csv"):
            if re.fullmatch(r"(?:[0-9]{4}|_unknown)\.csv", target.name):
                if target.stem not in counts and (allowed is None or target.stem in allowed):
                    target.unlink()
    return dict(sorted(counts.items()))


def append_rows(outdir, header, rows):
    """Append incoming rows to their year shards, validating existing headers."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        writers = {}
        for row in rows:
            year = year_of(row)
            if year not in writers:
                target = outdir / f"{year}.csv"
                new_file = not target.exists() or target.stat().st_size == 0
                if not new_file and read_header(target) != list(header):
                    raise ValueError(f"CSV header mismatch: {target}")
                stream = stack.enter_context(target.open("a", encoding="utf-8-sig", newline=""))
                writers[year] = csv.DictWriter(stream, fieldnames=header, lineterminator="\n")
                if new_file:
                    writers[year].writeheader()
            writers[year].writerow(row)


def concat(source, stream):
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(read_header(source))
    count = 0
    for row in iter_rows(source):
        writer.writerow(row)
        count += 1
    return count


def verify(original, directory):
    """Verify lossless row multisets, headers, BOMs and year partitioning."""
    header = read_header(original)
    if read_header(directory) != header:
        raise ValueError(f"Header differs from {original}")
    counts = {}
    for path in csv_paths(directory):
        with path.open("rb") as stream:
            if stream.read(3) != b"\xef\xbb\xbf":
                raise ValueError(f"Missing UTF-8 BOM: {path}")
        counts[path.name] = 0
        for row in iter_rows(path):
            if year_of(dict(zip(header, row))) != path.stem:
                raise ValueError(f"Row in wrong year shard: {path}")
            counts[path.name] += 1
    before = Counter(tuple(row) for row in iter_rows(original))
    after = Counter(tuple(row) for row in iter_rows(directory))
    if before != after:
        raise ValueError(f"Data rows differ: {original} vs {directory}")
    for name, count in counts.items():
        print(f"  {name}: {count} rows")
    print(f"MATCH {original}: {len(counts)} files, {sum(counts.values())} rows "
          f"(original {sum(before.values())}); exact row multiset")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    split = commands.add_parser("split", help="partition a flat CSV by year")
    split.add_argument("source", type=Path)
    split.add_argument("outdir", type=Path)
    join = commands.add_parser("concat", help="concatenate shards with one header")
    join.add_argument("directory", type=Path)
    join.add_argument("--out", type=Path, help="flat output (default: stdout), UTF-8 with BOM")
    check = commands.add_parser("verify", help="check exact row equivalence before deleting a flat CSV")
    check.add_argument("original", type=Path)
    check.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "split":
        counts = write_shards(args.outdir, read_header(args.source), iter_rows(args.source))
        for year, count in counts.items():
            print(f"  {year}.csv: {count} rows")
        print(f"{len(counts)} files, {sum(counts.values())} rows → {args.outdir}")
    elif args.command == "verify":
        verify(args.original, args.directory)
    elif args.out:
        # Refuse to overwrite a source shard while it is being read.
        if args.out.resolve() in {p.resolve() for p in csv_paths(args.directory)}:
            parser.error("--out must not overwrite an input shard")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8-sig", newline="") as stream:
            concat(args.directory, stream)
    else:
        stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8-sig", newline="")
        try:
            concat(args.directory, stream)
            stream.flush()
        finally:
            stream.detach()


if __name__ == "__main__":
    main()
