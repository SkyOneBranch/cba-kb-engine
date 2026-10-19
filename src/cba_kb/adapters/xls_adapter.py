"""Read-only adapter for legacy .xls (BIFF) workbooks.

Issue #198 (T2-2, AC-13~16): controlled read path for real-data canary
inputs such as the team roster workbook. Mirrors
``foreign_xlsx.rows(path, sheet=None)`` shape: ``rows()`` returns
``(sheet_name, table)`` of plain values, while ``read_cells()`` additionally
exposes each cell's raw value, xlrd type and merged-cell provenance so that
date serials are converted WITHOUT losing the original bytes.

Fail-closed: anything that is not a readable BIFF workbook (missing file,
plain text, OOXML .xlsx, corrupt stream) raises ``XlsFormatError`` or
``FileNotFoundError`` — never a half-parsed table.
"""
from pathlib import Path
from typing import List, NamedTuple, Tuple
import datetime

import xlrd
from xlrd.xldate import xldate_as_datetime

METHOD = 'xls BIFF read-only + merged-cell forward-fill + date serial conversion'

# Re-exported so callers/tests need not import xlrd directly.
XL_CELL_DATE = xlrd.XL_CELL_DATE


class XlsFormatError(Exception):
    """Input is not a readable .xls (BIFF) workbook. Fail closed."""


class XlsCell(NamedTuple):
    value: object  # parsed value; Excel date serials -> date (pure) or datetime (with time)
    raw: object    # original xlrd value; date serials kept as float
    ctype: int     # xlrd cell type (XL_CELL_*)
    merged: bool   # True when forward-filled from a merged range's top-left
    row: int       # 0-based row index in the sheet
    col: int       # 0-based column index in the sheet


class Rc04Record(NamedTuple):
    """One validated RC-04 roster record (evidence-preserving).

    Built only by read_rc04_records() from read_cells() evidence.
    person_id is always None and identity_status always 'candidate':
    this adapter never infers person identity.
    """
    seq: object          # 序号 (as read)
    name: object         # 姓名 (as read)
    dob: object          # 出生日期: datetime.date (date-only; never datetime)
    dob_raw: object      # original Excel serial (float) for the dob cell
    source_row: int      # 0-based row index in the sheet
    sheet: str           # sheet name
    person_id: object    # always None (no inference)
    identity_status: str # always 'candidate'


def _open(path):
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f'.xls source not found: {p}')
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise XlsFormatError(f'Cannot read .xls source: {p}: {exc}') from exc
    try:
        # formatting_info=True is required for merged_cells.
        return xlrd.open_workbook(file_contents=data, formatting_info=True)
    except Exception as exc:  # corrupt stream, OOXML, plain text, ...
        raise XlsFormatError(f'Not a readable .xls (BIFF) file: {p}: {exc}') from exc


def _parse(xl_cell, datemode):
    ctype, raw = xl_cell.ctype, xl_cell.value
    if ctype == xlrd.XL_CELL_DATE:
        try:
            dt = xldate_as_datetime(raw, datemode)
            # B2: never silently truncate time. Pure dates (midnight) keep
            # the historical date-only behavior; any non-zero time component
            # is preserved as a full datetime. Downstream schema validation
            # (e.g. RC-04) decides whether time-bearing values are allowed.
            if dt.hour or dt.minute or dt.second or dt.microsecond:
                value = dt
            else:
                value = dt.date()
        except Exception:
            # Unparseable serial: keep the raw number, never guess a date.
            value = raw
    elif ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        value = None
    else:
        value = raw
    return value, raw, ctype


def read_cells(path, sheet=None):
    """Return ``(sheet_name, grid)`` of :class:`XlsCell` (raw-preserving).

    Merged ranges are forward-filled from their top-left cell and flagged
    with ``merged=True``; empty rows are kept, never silently dropped.
    """
    book = _open(path)
    names = book.sheet_names()
    if not names:
        raise XlsFormatError(f'.xls workbook has no sheets: {path}')
    name = sheet or names[0]
    if name not in names:
        raise ValueError(f'Sheet not found: {name!r} (available: {names})')
    sh = book.sheet_by_name(name)
    fill_from = {}
    for (rlo, rhi, clo, chi) in sh.merged_cells:
        for r in range(rlo, rhi):
            for c in range(clo, chi):
                if (r, c) != (rlo, clo):
                    fill_from[(r, c)] = (rlo, clo)
    grid: List[List[XlsCell]] = []
    for r in range(sh.nrows):
        row = []
        for c in range(sh.ncols):
            if (r, c) in fill_from:
                r0, c0 = fill_from[(r, c)]
                value, raw, ctype = _parse(sh.cell(r0, c0), book.datemode)
                row.append(XlsCell(value, raw, ctype, True, r, c))
            else:
                value, raw, ctype = _parse(sh.cell(r, c), book.datemode)
                row.append(XlsCell(value, raw, ctype, False, r, c))
        grid.append(row)
    return name, grid


def rows(path, sheet=None):
    """Return ``(sheet_name, table)`` of plain values, like ``foreign_xlsx.rows``.

    B1: convenience interface only — it discards raw serials, cell types,
    merged-cell provenance and positions. It is kept for backward
    compatibility but MUST NOT be used for formal RC-04 ingestion; use
    ``read_cells()`` or ``read_rc04_records()`` instead.
    """
    name, grid = read_cells(path, sheet)
    return name, [[cell.value for cell in row] for row in grid]


# --- B3: thin RC-04 structural validation on top of the generic reader ---

RC04_REQUIRED_HEADERS = ('序号', '姓名', '出生日期')
_RC04_HEADER_SCAN_ROWS = 3  # tolerate merged title rows above the header


def _rc04_find_header(grid):
    """Locate the RC-04 header row within the first few rows.

    Returns (header_index, seq_col, name_col, dob_col) or None.
    Header cells match exactly after stripping surrounding whitespace;
    column order may vary.
    """
    for i in range(min(_RC04_HEADER_SCAN_ROWS, len(grid))):
        labels = [c.value.strip() if isinstance(c.value, str) else ''
                  for c in grid[i]]
        try:
            seq_c = labels.index(RC04_REQUIRED_HEADERS[0])
            name_c = labels.index(RC04_REQUIRED_HEADERS[1])
            dob_c = labels.index(RC04_REQUIRED_HEADERS[2])
        except ValueError:
            continue
        return i, seq_c, name_c, dob_c
    return None


def read_rc04_records(path, sheet=None):
    """Read and validate RC-04 roster records (fail-closed).

    B3: thin business validation reusing the generic ``read_cells()``
    evidence path. This is NOT a generic schema framework — it validates
    only the RC-04 roster shape required this round:

    - header row with 序号 / 姓名 / 出生日期 (first 3 rows scanned, so a
      merged title row above the header is tolerated; column order free);
    - at least one non-empty data row (empty business data is rejected);
    - every birth-date cell must be a pure date: a datetime with a non-zero
      time component, or any non-date value, rejects the whole table
      (fail-closed; no silent truncation, no per-row guessing).

    Returns ``(sheet_name, [Rc04Record, ...])``. Each record keeps the raw
    serial, source row and sheet. ``person_id`` is always None and
    ``identity_status`` always ``'candidate'`` — no identity inference.

    Raises ``ValueError`` on wrong header, empty data, or uninterpretable
    records; ``XlsFormatError``/``FileNotFoundError`` from the reader pass
    through unchanged.
    """
    name, grid = read_cells(path, sheet)
    found = _rc04_find_header(grid)
    if found is None:
        raise ValueError(
            f'RC-04 header not found (need 序号/姓名/出生日期): {path}')
    header_idx, seq_c, name_c, dob_c = found
    records: List[Rc04Record] = []
    for r in range(header_idx + 1, len(grid)):
        row = grid[r]
        if not any(c.value is not None for c in row):
            continue  # blank spacer rows are not records
        # Guard against ragged rows.
        if max(seq_c, name_c, dob_c) >= len(row):
            raise ValueError(
                f'RC-04 row {r} is ragged (missing columns): {path}')
        seq_cell, name_cell, dob_cell = row[seq_c], row[name_c], row[dob_c]
        # B3-R2-1: seq and name must be non-blank. A record with a valid
        # date but missing seq/name is structurally incomplete.
        seq_val = seq_cell.value
        name_val = name_cell.value
        if seq_val is None or (isinstance(seq_val, str) and not seq_val.strip()):
            raise ValueError(
                f'RC-04 row {r} has blank seq; refusing table: {path}')
        if name_val is None or (isinstance(name_val, str) and not name_val.strip()):
            raise ValueError(
                f'RC-04 row {r} has blank name; refusing table: {path}')
        dob_val = dob_cell.value
        # Birth date must be a pure date. A datetime with time, or a
        # non-date value, means the record cannot be reliably interpreted.
        if isinstance(dob_val, datetime.datetime):
            raise ValueError(
                f'RC-04 row {r} birth date has a time component '
                f'({dob_val!r}); refusing table: {path}')
        if not isinstance(dob_val, datetime.date):
            raise ValueError(
                f'RC-04 row {r} birth date is not a date '
                f'({dob_val!r}); refusing table: {path}')
        records.append(Rc04Record(
            seq=seq_cell.value,
            name=name_cell.value,
            dob=dob_val,
            dob_raw=dob_cell.raw,
            source_row=r,
            sheet=name,
            person_id=None,
            identity_status='candidate',
        ))
    if not records:
        raise ValueError(f'RC-04 has no data rows: {path}')
    return name, records
