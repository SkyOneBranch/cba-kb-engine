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

import xlrd
from xlrd.xldate import xldate_as_datetime

METHOD = 'xls BIFF read-only + merged-cell forward-fill + date serial conversion'

# Re-exported so callers/tests need not import xlrd directly.
XL_CELL_DATE = xlrd.XL_CELL_DATE


class XlsFormatError(Exception):
    """Input is not a readable .xls (BIFF) workbook. Fail closed."""


class XlsCell(NamedTuple):
    value: object  # parsed value; Excel date serials -> datetime.date
    raw: object    # original xlrd value; date serials kept as float
    ctype: int     # xlrd cell type (XL_CELL_*)
    merged: bool   # True when forward-filled from a merged range's top-left


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
            value = xldate_as_datetime(raw, datemode).date()
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
                row.append(XlsCell(value, raw, ctype, True))
            else:
                value, raw, ctype = _parse(sh.cell(r, c), book.datemode)
                row.append(XlsCell(value, raw, ctype, False))
        grid.append(row)
    return name, grid


def rows(path, sheet=None):
    """Return ``(sheet_name, table)`` of plain values, like ``foreign_xlsx.rows``."""
    name, grid = read_cells(path, sheet)
    return name, [[cell.value for cell in row] for row in grid]
