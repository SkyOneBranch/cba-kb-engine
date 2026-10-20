"""Source adapters: probe a source, extract records, emit staging.

Adapters never write to Drive and never touch production data. Each adapter
reads one registered source and returns grain-separated records plus a QA report.

T2-8 integration (Issue #212): ``xls_adapter`` is registered for legacy
``.xls`` (BIFF) sources. ``muse_handoff`` (payload-in) and
``article_index`` (a projector over foreign_xlsx rows) do not fit the
path-probe contract, so they are exported here as integration surface
only and are deliberately NOT in ``ADAPTERS`` / ``probe``.
"""
from . import (
    article_index,
    cba_registration,
    foreign_image,
    foreign_xlsx,
    midseason_md,
    muse_handoff,
    xls_adapter,
)

ADAPTERS = {'midseason_md': midseason_md, 'foreign_xlsx': foreign_xlsx,
            'foreign_image': foreign_image, 'xls_adapter': xls_adapter}
WATCHER_ADAPTERS = {'cba_registration': cba_registration}


def watcher_adapter_for(name):
    if name not in WATCHER_ADAPTERS:
        raise ValueError(f'Unknown watcher adapter {name!r}')
    return WATCHER_ADAPTERS[name]


def adapter_for(name=None, path=None):
    if name:
        if name not in ADAPTERS:
            raise ValueError(f'Unknown adapter {name!r}')
        return ADAPTERS[name]
    if path is None:
        raise ValueError('Adapter or source path required')
    return ADAPTERS[probe(path)]


def probe(path):
    suffix = str(path).lower().rsplit('.', 1)[-1]
    if suffix == 'md':
        return 'midseason_md'
    if suffix in ('xlsx', 'xlsm'):
        return 'foreign_xlsx'
    if suffix == 'xls':
        return 'xls_adapter'
    if suffix in ('png', 'jpg', 'jpeg', 'pdf'):
        return 'foreign_image'
    if suffix in ('json', 'jsonl', 'csv'):
        return 'foreign_image'
    raise ValueError(f'No adapter claims {path}')
