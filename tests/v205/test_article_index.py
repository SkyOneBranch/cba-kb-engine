"""T2-5: RC-05 article index and data lineage (article_index).

Synthetic fixtures only (generated with openpyxl, fictional titles).
Real WeChat article content NEVER enters the repo (AC-29).

Covers AC-17~23, AC-25 shapes:
  - article -> candidate index: id/title/date mapped verbatim (AC-22)
  - article_id text preserved as-is; 序号 is never an article ID
  - every consumable candidate carries (source_file, source_sheet,
    source_row) lineage; records without lineage are never indexed
  - AC-25 authorized path: person -> Stats/candidate/articles ->
    UNVERIFIED association -> jump back to the exact source row
  - person_id is never assigned; identity stays 'candidate' (AC-23)
"""
from datetime import datetime

import pytest
from openpyxl import Workbook

from cba_kb.adapters import article_index, foreign_xlsx
from cba_kb.adapters.article_index import (
    ArticleIndexError,
    build_article_index,
    candidate_articles_for_person,
    find_by_article_id,
    resolve_lineage,
    search_by_title,
)

SOURCE_FILE = 'synthetic_rc05.xlsx'


def rc05_workbook(tmp_path, name=SOURCE_FILE):
    """Synthetic RC-05-shaped workbook (real header shape, fictional data).

    Headers: 序号 / 微信文章编号 / 文章标题 / 日期.
    """
    book = Workbook()
    sheet = book.active
    sheet.title = 'Sheet1'
    sheet.append(['序号', '微信文章编号', '文章标题', '日期'])
    sheet.append([1, 'WX001', '虚构球员甲生日快乐', '2025/03/05'])
    sheet.append([2, 'WX-007', '大狙专访：虚构后卫谈赛季', datetime(2025, 3, 9)])
    sheet.append([3, '001', '战报：虚构球队甲获胜', 45705])
    sheet.append([4, 'WX099', '虚构球员乙生日快乐', None])
    path = tmp_path / name
    book.save(path)
    return path


def workbook_index(tmp_path):
    _, articles = foreign_xlsx.article_rows(rc05_workbook(tmp_path))
    return build_article_index(articles, source_file=SOURCE_FILE)


# --- construction / mapping (AC-17~22) -----------------------------

def test_index_maps_id_title_date(tmp_path):
    index = workbook_index(tmp_path)
    assert index['schema'] == 'article_index_v1'
    assert index['source_file'] == SOURCE_FILE
    assert index['rejected'] == []
    cands = index['candidates']
    assert len(cands) == 4
    by_id = {c['article_id']: c for c in cands}
    assert by_id['WX001']['title'] == '虚构球员甲生日快乐'
    assert by_id['WX001']['row_date_text'] == '2025/03/05'
    assert by_id['WX-007']['row_date_text'] == '2025-03-09T00:00:00'


def test_article_id_text_preserved_verbatim(tmp_path):
    index = workbook_index(tmp_path)
    ids = {c['article_id'] for c in index['candidates']}
    # No numeric normalization, no prefix guessing: '001' stays '001',
    # 'WX-007' keeps its dash and zero padding.
    assert ids == {'WX001', 'WX-007', '001', 'WX099'}


def test_serial_column_never_used_as_article_id(tmp_path):
    index = workbook_index(tmp_path)
    ids = {c['article_id'] for c in index['candidates']}
    # 序号 values are 1..4 (row ordinals), never article identities.
    assert '1' not in ids and '2' not in ids and '4' not in ids


def test_date_semantics_never_inferred(tmp_path):
    index = workbook_index(tmp_path)
    by_id = {c['article_id']: c for c in index['candidates']}
    serial = by_id['001']
    # Excel serial 45705 carried as ISO text; meaning never inferred.
    assert serial['row_date_text'] == '2025-02-17'
    assert serial['row_date_raw'] == 45705
    for cand in index['candidates']:
        assert cand['date_interpretation'] is None
        assert cand['date_verified'] is False


def test_identity_stays_candidate(tmp_path):
    index = workbook_index(tmp_path)
    for cand in index['candidates']:
        assert cand['identity_status'] == 'candidate'
        assert cand['person_id'] is None


# --- lineage (AC-25 foundation) ------------------------------------

def test_every_candidate_carries_full_lineage(tmp_path):
    index = workbook_index(tmp_path)
    for cand in index['candidates']:
        lineage = cand['lineage']
        assert lineage['source_file'] == SOURCE_FILE
        assert lineage['source_sheet'] == 'Sheet1'
        assert isinstance(lineage['source_row'], int)
        assert lineage['source_row'] >= 2


def test_lineage_resolves_to_exact_source_row(tmp_path):
    index = workbook_index(tmp_path)
    cand = find_by_article_id(index, 'WX001')
    assert resolve_lineage(cand) == {
        'source_file': SOURCE_FILE, 'source_sheet': 'Sheet1', 'source_row': 2}
    assert resolve_lineage(find_by_article_id(index, '001'))['source_row'] == 4
    assert resolve_lineage(find_by_article_id(index, 'WX099'))['source_row'] == 5


def test_record_without_lineage_never_indexed():
    base = {
        'article_id': 'WX500', 'title': '无血缘文章', 'row_date_text': '',
        'row_date_raw': None, 'date_interpretation': None,
        'date_verified': False, 'identity_status': 'candidate',
        'person_id': None, 'source_sheet': 'Sheet1', 'source_row': 7,
    }
    no_row = dict(base, source_row=None)
    no_sheet = dict(base, source_sheet='')
    ok = dict(base, article_id='WX501')
    index = build_article_index([no_row, no_sheet, ok], source_file=SOURCE_FILE)
    assert [c['article_id'] for c in index['candidates']] == ['WX501']
    assert len(index['rejected']) == 2
    assert all(r['reason'] == 'ARTICLE_NO_LINEAGE' for r in index['rejected'])


def test_empty_title_rejected():
    rec = {
        'article_id': 'WX600', 'title': '', 'row_date_text': '',
        'row_date_raw': None, 'date_interpretation': None,
        'date_verified': False, 'identity_status': 'candidate',
        'person_id': None, 'source_sheet': 'Sheet1', 'source_row': 3,
    }
    index = build_article_index([rec], source_file=SOURCE_FILE)
    assert index['candidates'] == []
    assert index['rejected'][0]['reason'] == 'ARTICLE_NO_TITLE'


def test_duplicate_source_row_raises():
    rec = {
        'article_id': 'WX700', 'title': '重复行文章', 'row_date_text': '',
        'row_date_raw': None, 'date_interpretation': None,
        'date_verified': False, 'identity_status': 'candidate',
        'person_id': None, 'source_sheet': 'Sheet1', 'source_row': 3,
    }
    with pytest.raises(ArticleIndexError, match='DUPLICATE'):
        build_article_index([rec, dict(rec, article_id='WX701')],
                            source_file=SOURCE_FILE)


def test_invalid_build_inputs_raise():
    with pytest.raises(ArticleIndexError):
        build_article_index('not-a-list', source_file=SOURCE_FILE)
    with pytest.raises(ArticleIndexError):
        build_article_index([], source_file='')
    with pytest.raises(ArticleIndexError):
        build_article_index([], source_file=None)


# --- retrieval ------------------------------------------------------

def test_find_by_article_id(tmp_path):
    index = workbook_index(tmp_path)
    assert find_by_article_id(index, 'WX-007')['title'] == '大狙专访：虚构后卫谈赛季'
    assert find_by_article_id(index, 'NOPE') is None


def test_search_by_title_substring(tmp_path):
    index = workbook_index(tmp_path)
    hits = search_by_title(index, '生日快乐')
    assert {c['article_id'] for c in hits} == {'WX001', 'WX099'}
    assert search_by_title(index, '不存在的词') == []


# --- AC-25 authorized path ------------------------------------------

def test_ac25_person_to_unverified_article_to_source(tmp_path):
    index = workbook_index(tmp_path)
    # Synthetic authorized context: a person known to Stats/candidates.
    person = {'name': '虚构球员甲', 'identity_status': 'candidate',
              'person_id': None}
    associations = candidate_articles_for_person(index, person['name'])
    assert len(associations) == 1
    assoc = associations[0]
    # The person->article link is pending verification, never confirmed.
    assert assoc['association_status'] == 'UNVERIFIED'
    assert assoc['person_name'] == '虚构球员甲'
    cand = assoc['candidate']
    assert cand['article_id'] == 'WX001'
    assert cand['person_id'] is None
    # Final hop: jump back to the exact original row.
    assert resolve_lineage(cand) == {
        'source_file': SOURCE_FILE, 'source_sheet': 'Sheet1', 'source_row': 2}


def test_person_without_articles_gets_no_associations(tmp_path):
    index = workbook_index(tmp_path)
    assert candidate_articles_for_person(index, '查无此人') == []


def test_r2_whitespace_lineage_rejected():
    """B-01: 空白 sheet/file 不是血缘。"""
    rec = {"article_id": "WX001", "title": "S", "source_sheet": "   ",
           "source_row": 2}
    idx = build_article_index([rec], source_file="synthetic.xlsx")
    assert idx["candidates"] == []
    assert idx["rejected"][0]["reason"] == "ARTICLE_NO_LINEAGE"
    with pytest.raises(ArticleIndexError):
        build_article_index([rec], source_file="   ")


def test_r2_duplicate_escapes_via_rejected_record():
    """B-02: 被拒记录也占位，同行第二条必须报重复。"""
    base = {"article_id": "WX001", "source_sheet": "Sheet1", "source_row": 3}
    with pytest.raises(ArticleIndexError):
        build_article_index(
            [{**base, "title": ""},
             {**base, "article_id": "WX002", "title": "Valid"}],
            source_file="s.xlsx")


def test_r2_index_id_no_delimiter_ambiguity():
    """B-03: ("a|b","c") 与 ("a","b|c") 不得同 ID。"""
    def mk(sheet):
        return [{"article_id": "WX001", "title": "S",
                 "source_sheet": sheet, "source_row": 2}]
    a = build_article_index(mk("c"), source_file="a|b")
    b = build_article_index(mk("b|c"), source_file="a")
    assert a["candidates"][0]["index_id"] != b["candidates"][0]["index_id"]


def test_r3_index_id_no_normalization_conflation():
    """B-03/R3: 归一化会混同的坐标必须 ID 不同、同批构建不误报重复。"""
    def record(sheet):
        return {"article_id": "WX001", "title": "Synthetic",
                "source_sheet": sheet, "source_row": 2}
    def cand(sheet):
        return build_article_index(
            [record(sheet)], source_file="synthetic.xlsx"
        )["candidates"][0]
    # 全角 vs 半角
    assert cand("A")["index_id"] != cand("Ａ")["index_id"]
    # 单空格 vs 双空格
    assert cand("a b")["index_id"] != cand("a  b")["index_id"]
    # 同批构建不误报重复
    idx = build_article_index([record("A"), record("Ａ")],
                              source_file="synthetic.xlsx")
    assert len(idx["candidates"]) == 2
    # 相同坐标仍确定性判重
    assert cand("A")["index_id"] == cand("A")["index_id"]
    with pytest.raises(ArticleIndexError):
        build_article_index([record("A"), record("A")],
                            source_file="synthetic.xlsx")
