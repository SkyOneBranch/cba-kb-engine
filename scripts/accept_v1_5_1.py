"""Formal-ID acceptance of the published v1.5.1 fact products (Codex side).

Reads only the production objects by their Drive IDs, exactly as an external AI
consumer would, and writes workspace/reports/<release>-acceptance-codex.json.
"""
import argparse
import io
import json
import os
import sys
from pathlib import Path
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from cba_kb.common import digest, save
from cba_kb.drive import Drive
from cba_kb.instance import load_instance
from cba_kb.master import HEADERS, resolve_master_file_id
from cba_kb.native import BEGIN, END


def _require_bound_id(explicit, env_var, label):
    val = (explicit or os.environ.get(env_var) or '').strip()
    if not val or (val.startswith('<') and val.endswith('>')):
        raise ValueError(f'ACCEPT_V1_5_1_BOUND_ID_REQUIRED:{label} (pass CLI arg or set {env_var})')
    return val


def sheet(blob, name):
    book = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    page = book[book.sheetnames[0]]
    values = list(page.values)
    head = list(values[0])
    return book.sheetnames, head, [dict(zip(head, row)) for row in values[1:]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', default='v1.5.1-1')
    parser.add_argument('--report', type=Path, default=None)
    parser.add_argument('--instance-root', type=Path, default=None)
    parser.add_argument('--master-id', default=None)
    parser.add_argument('--snapshots-id', default=None)
    parser.add_argument('--events-id', default=None)
    parser.add_argument('--status-id', default=None)
    parser.add_argument('--registry-id', default=None)
    parser.add_argument('--index-id', default=None)
    parser.add_argument('--expected-source-id', default=None)
    parser.add_argument('--baseline', type=Path,
                        default=Path('workspace/inputs/oauth-001/MASTER.xlsx'))
    arguments = parser.parse_args()
    instance = (
        load_instance(Path.cwd(), arguments.instance_root)
        if (arguments.instance_root or os.environ.get('CBA_KB_INSTANCE_ROOT'))
        else None
    )
    master_id = resolve_master_file_id(
        arguments.master_id, instance=instance, require_bound=True
    )
    snapshots_id = _require_bound_id(arguments.snapshots_id, 'CBA_KB_SNAPSHOTS_FILE_ID', 'SNAPSHOTS_ID')
    events_id = _require_bound_id(arguments.events_id, 'CBA_KB_EVENTS_FILE_ID', 'EVENTS_ID')
    status_id = _require_bound_id(arguments.status_id, 'CBA_KB_STATUS_FILE_ID', 'STATUS_ID')
    registry_id = _require_bound_id(arguments.registry_id, 'CBA_KB_REGISTRY_FILE_ID', 'REGISTRY_ID')
    index_id = _require_bound_id(arguments.index_id, 'CBA_KB_INDEX_FILE_ID', 'INDEX_ID')
    expected_source_id = _require_bound_id(
        arguments.expected_source_id, 'CBA_KB_EXPECTED_SOURCE_ID', 'EXPECTED_SOURCE_ID'
    )
    report_path = arguments.report or Path('workspace/reports')/f'{arguments.release}-acceptance-codex.json'
    drive = Drive(Path.cwd(), instance)
    checks, evidence = [], {}

    def check(name, passed, detail):
        checks.append({'check': name, 'status': 'PASS' if passed else 'FAIL', 'detail': detail})

    status = json.loads(drive.get(status_id))
    evidence['release_status'] = {'state': status.get('state'),
                                  'current_release_id': status.get('current_release_id'),
                                  'previous_release_id': status.get('previous_release_id'),
                                  'artifacts': len(status.get('artifacts', [])),
                                  'url': f'https://drive.google.com/file/d/{status_id}/view'}
    check('release_status is COMPLETE for this release',
          status.get('state') == 'COMPLETE' and status.get('current_release_id') == arguments.release,
          evidence['release_status'])

    master_blob = drive.get(master_id)
    names, head, master = sheet(master_blob, 'MASTER')
    baseline_names, baseline_head, baseline = sheet(arguments.baseline.read_bytes(), 'MASTER')
    baseline_doc = json.loads((Path('docs/baseline_2026-09-10.json')).read_text())
    evidence['master'] = {'url': f'https://drive.google.com/file/d/{master_id}/view',
                          'rows': len(master), 'columns': len(head), 'sha256': digest(master_blob),
                          'unique_keys': len({row['record_key'] for row in master})}
    check('MASTER schema unchanged (20 columns)', head == list(HEADERS), head)
    check('MASTER carries baseline rows verbatim',
          all(all(row[key] == base[key] for key in head) for row, base in zip(master, baseline)),
          f'{len(baseline)} baseline rows compared')
    check('MASTER keys unique and rows = baseline + additions',
          len(master) == len(baseline) + 14 and len({row['record_key'] for row in master}) == len(master),
          f'rows={len(master)}')
    jiahao = [row for row in master if row['player'] == '贾昊' and row['season'] == '2024-2025']
    check('Jia Hao deadline regression',
          jiahao and jiahao[0]['notice_deadline'] == baseline_doc['jiahao'],
          jiahao[0]['notice_deadline'] if jiahao else 'record missing')

    snapshot_names, snapshot_head, snapshots = sheet(drive.get(snapshots_id), 'SNAPSHOTS')
    evidence['snapshots'] = {'url': f'https://drive.google.com/file/d/{snapshots_id}/view',
                             'sheets': snapshot_names, 'rows': len(snapshots), 'columns': len(snapshot_head),
                             'unique_keys': len({row['snapshot_key'] for row in snapshots}),
                             'seasons': sorted({row['season'] for row in snapshots})}
    check('SNAPSHOTS single sheet, 73 rows, unique keys',
          snapshot_names == ['SNAPSHOTS'] and len(snapshots) == 73
          and len({row['snapshot_key'] for row in snapshots}) == 73,
          evidence['snapshots'])
    check('SNAPSHOTS keeps raw and normalized names plus text jersey numbers',
          all(isinstance(row['jersey_number'], str) for row in snapshots
              if row['jersey_number'] is not None)
          and any(row['name_en_raw'] != row['name_en_normalized'] for row in snapshots)
          and all(row['source_file_id'] for row in snapshots),
          [(row['name_en_raw'], row['name_en_normalized']) for row in snapshots
           if row['name_en_raw'] != row['name_en_normalized']])

    event_names, event_head, events = sheet(drive.get(events_id), 'EVENTS')
    cancellations = [row for row in events if row['event_type'] == 'registration_cancelled']
    changes = [row for row in events if row['event_type'] == 'registration_change']
    evidence['events'] = {'url': f'https://drive.google.com/file/d/{events_id}/view',
                          'sheets': event_names, 'rows': len(events),
                          'unique_keys': len({row['event_key'] for row in events}),
                          'cancellations': len(cancellations), 'changes': len(changes)}
    check('EVENTS single sheet, 73 rows, unique keys, 59 + 14 split',
          event_names == ['EVENTS'] and len(events) == 73
          and len({row['event_key'] for row in events}) == 73
          and len(cancellations) == 59 and len(changes) == 14,
          evidence['events'])
    check('Cancellations have dates with inferred-year flag; no fabricated timestamps',
          all(row['event_date'] and row['date_year_inferred'] is True for row in cancellations)
          and all(len(row['event_date']) == 10 for row in events),
          sorted({row['event_date'] for row in cancellations})[:3])
    split = [{'player': row['player_name_zh'], 'transaction': row['event_date'],
              'announcement': row['club_announcement_date']} for row in changes
             if row['club_announcement_date'] and row['event_date'] != row['club_announcement_date']]
    check('Midseason window deadline stays separate from notice_deadline',
          {row['registration_window_deadline'] for row in changes} == {'2021-02-27'}
          and all(row['club_announcement_date'] for row in changes) and bool(split),
          {'window_deadline': sorted({row['registration_window_deadline'] for row in changes}),
           'announcement_dates_distinct_from_transaction': split})
    check('Snapshot and event grains stay separate',
          not ({row['snapshot_key'] for row in snapshots} & {row['event_key'] for row in events})
          and 'snapshot_key' not in event_head and 'event_key' not in snapshot_head,
          'no key overlap')

    registry = drive.get(registry_id).decode('utf-8-sig').strip().splitlines()
    evidence['registry'] = {'url': f'https://drive.google.com/file/d/{registry_id}/view',
                            'rows': len(registry) - 1}
    check('Source registry registered the remaining sources',
          all(any(source in line for line in registry)
              for source in (expected_source_id,)),
          evidence['registry'])

    prefix = drive.get_managed_doc(index_id).decode()
    evidence['index'] = {'url': f'https://docs.google.com/document/d/{index_id}/edit',
                         'prefix_bytes': len(prefix.encode())}
    check('INDEX entry point exposes the v1.5.1 products and coverage limits',
          prefix.lstrip().startswith(BEGIN.strip()) and END.strip() in prefix
          and 'CBA_外籍球员注册_SNAPSHOTS.xlsx' in prefix and 'CBA_球员注册_EVENTS.xlsx' in prefix
          and '尚未完成验收的来源' in prefix,
          'managed prefix inspected')

    report = {'release': arguments.release, 'consumer': 'codex (local OAuth, formal Drive IDs)',
              'state': 'PASS' if all(item['status'] == 'PASS' for item in checks) else 'FAIL',
              'checks': checks, 'evidence': evidence,
              'note': ('Formal-ID consumption acceptance. It does not replace the second AI result '
                       'and does not by itself authorise a STABLE tag.')}
    save(report_path, report)
    print(json.dumps({'report': str(report_path), 'state': report['state'],
                      'checks': {item['check']: item['status'] for item in checks}},
                     ensure_ascii=False, indent=2))
    return 0 if report['state'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
