"""Adapter for the CBA foreign player registration workbook and birthday-article workbooks."""
from datetime import date, datetime
from pathlib import Path
import re
from openpyxl import load_workbook
from . import foreign

METHOD = 'xlsx merge-header segmentation + season-aware cancellation parsing'


def rows(path, sheet=None):
    book = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        name = sheet or book.sheetnames[0]
        if book.sheetnames != [name]:
            raise ValueError('Foreign source must stay a single-sheet file')
        cells = list(book[name].iter_rows())
        table = [[cell.value for cell in row] for row in cells]
        index = foreign.header_index(table)
        jersey = foreign.columns(table, index)['球衣号码']
        for number in range(index + 2, len(cells)):
            cell = cells[number][jersey]
            value = cell.value
            # A numeric zero displayed with the format 00 is a text code 00.
            # Keep General-format zero as 0, and do not coerce source strings.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if re.fullmatch(r'0+', cell.number_format or '') and value == int(value):
                    table[number][jersey] = str(int(value)).zfill(len(cell.number_format))
        return name, table
    finally:
        book.close()


def extract(path, season, source, clubs=None, strict=True, sheet=None):
    page, table = rows(path, sheet)
    title = str(table[0][0]) if table and table[0] and table[0][0] else None
    source = {**source, 'method': source.get('method') or METHOD}
    return foreign.build(table, season, source, clubs=clubs, strict=strict,
                         title=title, source_page=page)


# --- Birthday-article workbooks (RC-05) --------------------------------------
# Multi-sheet article workbooks: one article-data sheet plus pivot/summary
# sheets. Pivot/summary/unknown sheets are classification-only and never emit
# fact rows. AC-22: dates are carried verbatim, never interpreted as birthdays
# or publish times. AC-23: nicknames/aliases/titles are retrieval candidates
# only; a Person ID is never assigned by this adapter.

ARTICLE_DATA = 'article_data'
PIVOT = 'pivot'
SUMMARY = 'summary'
SHEET_UNKNOWN = 'unknown'

ARTICLE_ID_HEADERS = ('文章编号', '编号', 'id', 'ID', '文章ID')
ARTICLE_TITLE_HEADERS = ('标题', '题目', 'title')
ARTICLE_DATE_HEADERS = ('日期', 'date', '时间')
PIVOT_MARKERS = ('数据透视', '透视表', '汇总', '总计', '计数', 'Grand Total')
SUMMARY_MARKERS = ('统计', '平均', '合计')


def _cell_text(value):
    return str(value).strip() if value is not None else ''


def _find_article_header(rows):
    """Shared article-header detector (B2): scan probed text rows 1-12.

    Returns (row_index_0based, id_index, title_index, date_index) or None.
    Used by both _classify_sheet and _article_columns so the two agree on
    which row is the header, including when a title row sits above it.
    """
    for index, cells in enumerate(rows):
        id_index = next((i for i, cell in enumerate(cells) if cell in ARTICLE_ID_HEADERS), None)
        title_index = next((i for i, cell in enumerate(cells) if cell in ARTICLE_TITLE_HEADERS), None)
        if id_index is not None and title_index is not None:
            date_index = next((i for i, cell in enumerate(cells) if cell in ARTICLE_DATE_HEADERS), None)
            return index, id_index, title_index, date_index
    return None


def _classify_sheet(sheet):
    probe = [[_cell_text(value) for value in row]
             for row in sheet.iter_rows(min_row=1, max_row=12, values_only=True)]
    flat = [cell for row in probe for cell in row if cell]
    if not flat:
        return SHEET_UNKNOWN
    header = _find_article_header(probe)
    # B1: pivot/summary markers take precedence, fail-closed. A sheet carrying
    # markers is NEVER article data; markers + article-like header coexisting
    # is ambiguous -> unknown (never emitted as facts).
    has_pivot = any(any(marker in cell for marker in PIVOT_MARKERS) for cell in flat)
    has_summary = any(any(marker in cell for marker in SUMMARY_MARKERS) for cell in flat)
    if (has_pivot or has_summary) and header is not None:
        return SHEET_UNKNOWN
    if has_pivot:
        return PIVOT
    if has_summary:
        return SUMMARY
    if header is not None:
        return ARTICLE_DATA
    return SHEET_UNKNOWN


def classify_sheets(path):
    """Classify every sheet of a birthday-article workbook.

    Returns {sheet_name: role} with role in
    {'article_data', 'pivot', 'summary', 'unknown'}. Only 'article_data'
    sheets may be read with article_rows(); all other roles never emit facts.
    """
    book = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        return {name: _classify_sheet(book[name]) for name in book.sheetnames}
    finally:
        book.close()


def _article_columns(sheet):
    probe = [[_cell_text(value) for value in row]
             for row in sheet.iter_rows(min_row=1, max_row=12, values_only=True)]
    found = _find_article_header(probe)
    if found is None:
        raise ValueError('Article header row not found (need id-like and title-like columns)')
    index, id_index, title_index, date_index = found
    return index + 1, id_index, title_index, date_index


def _date_text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _article_record(values, columns, sheet_name, row_number):
    _, id_index, title_index, date_index = columns

    def at(index):
        return values[index] if index is not None and index < len(values) else None

    raw_id, raw_title, raw_date = at(id_index), at(title_index), at(date_index)
    return {
        'article_id_raw': raw_id,
        'article_id': _cell_text(raw_id) or None,
        'title_raw': raw_title,
        'title': _cell_text(raw_title),
        'row_date_raw': raw_date,
        'row_date_text': _date_text(raw_date),
        # AC-22: dates are carried verbatim; interpretation is never inferred here.
        'date_interpretation': None,
        'date_verified': False,
        # AC-23: nicknames/aliases/titles are retrieval candidates only;
        # a Person ID is never assigned by this adapter.
        'identity_status': 'candidate',
        'person_id': None,
        'source_sheet': sheet_name,
        'source_row': row_number,
    }


def article_rows(path, sheet=None):
    """Read article rows from the data sheet of a birthday-article workbook.

    Returns (sheet_name, [article, ...]). When sheet is omitted, the workbook
    must contain exactly one 'article_data' sheet; otherwise a ValueError is
    raised instead of guessing. Passing a pivot/summary sheet raises
    ValueError: those sheets never emit facts.
    """
    book = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        roles = {name: _classify_sheet(book[name]) for name in book.sheetnames}
        if sheet is not None:
            if sheet not in roles:
                raise ValueError(f'Sheet {sheet!r} not found')
            if roles[sheet] != ARTICLE_DATA:
                raise ValueError(f'Sheet {sheet!r} is {roles[sheet]}, not article data; '
                                 'pivot/summary sheets never emit facts')
            name = sheet
        else:
            data = [sheet_name for sheet_name, role in roles.items() if role == ARTICLE_DATA]
            if len(data) != 1:
                raise ValueError(f'Expected exactly one article-data sheet, found {len(data)}; '
                                 'pass sheet= explicitly')
            name = data[0]
        columns = _article_columns(book[name])
        header_row = columns[0]
        articles = []
        for number, row in enumerate(
                book[name].iter_rows(min_row=header_row + 1, values_only=True), header_row + 1):
            values = list(row)
            if not any(value is not None and _cell_text(value) for value in values):
                continue
            articles.append(_article_record(values, columns, name, number))
        return name, articles
    finally:
        book.close()
