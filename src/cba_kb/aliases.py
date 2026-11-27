"""Season-aware club name resolution. Unknown names fail closed."""
from pathlib import Path
from types import MappingProxyType
from typing import Optional
import unicodedata
import yaml

CONFIG = 'config/club_aliases.yaml'

# Authoritative CBA league official team_id -> Chinese club standard name (teams_2024 baseline).
CBA_TEAM_ID_TO_ZH: MappingProxyType[str, str] = MappingProxyType({
    '29115': '北京北汽',
    '29117': '新疆伊力特',
    '29118': '江苏肯帝亚',
    '29124': '广东东阳光',
    '29125': '上海久事',
    '29127': '四川丰谷酒业',
    '29128': '浙江方兴渡',
    '29129': '辽宁本钢',
    '29130': '山东高速',
    '29131': '深圳马可波罗',
    '29132': '山西汾酒',
    '29133': '南京头排苏酒',
    '29134': '福建晋江文旅',
    '29135': '青岛国信制药',
    '29136': '北京控股',
    '29137': '九台农商银行',
    '29138': '天津先行者',
    '29139': '广州朗肽海本',
    '29140': '浙江稠州金租',
    '100074683': '宁波町渥',
})

# Authoritative mapping from league team_id -> config/club_aliases.yaml canonical club_id.
CBA_TEAM_ID_TO_CANONICAL_ID: MappingProxyType[str, str] = MappingProxyType({
    '29115': 'beijing_shougang',
    '29117': 'xinjiang_guanghui',
    '29118': 'jiangsu_kendiya',
    '29124': 'guangdong_hongyuan',
    '29125': 'shanghai_jiushi',
    '29127': 'sichuan_jincheng',
    '29128': 'zhejiang_guangsha',
    '29129': 'liaoning_shenyang_sansheng',
    '29130': 'shandong_gaosu',
    '29131': 'shenzhen_xinshiji',
    '29132': 'shanxi_fenjiu',
    '29133': 'nanjing_tongxi',
    '29134': 'fujian_xunxing',
    '29135': 'qingdao_guoxin_haitian',
    '29136': 'beijing_konggu',
    '29137': 'jilin_jiutai',
    '29138': 'tianjin_ronggang',
    '29139': 'guangzhou_longshi',
    '29140': 'zhejiang_chouzhou',
    '100074683': 'ningbo_fubang',
})


def resolve_team_id_zh(team_id: object) -> Optional[str]:
    """Resolve an official CBA league team_id to its Chinese club standard name.

    Unknown, boolean, or empty team IDs return None (honest fallback; never guess).
    """
    if team_id is None or isinstance(team_id, bool):
        return None
    key = str(team_id).strip()
    if not key:
        return None
    return CBA_TEAM_ID_TO_ZH.get(key)


_CANONICAL_ID_TO_ZH: MappingProxyType[str, str] = MappingProxyType({
    canonical_id: CBA_TEAM_ID_TO_ZH[team_id]
    for team_id, canonical_id in CBA_TEAM_ID_TO_CANONICAL_ID.items()
    if team_id in CBA_TEAM_ID_TO_ZH
})


def resolve_cba_team_name(
    team_id_or_name: object,
    *,
    fallback_name: Optional[str] = None,
    prefer_short: bool = False,
) -> str:
    """Resolve a numeric CBA team_id or canonical club_id to its Chinese standard name.

    Falls back honestly to `fallback_name` (if non-empty) or the raw stripped input.
    """
    _ = prefer_short
    fb = fallback_name.strip() if isinstance(fallback_name, str) and fallback_name.strip() else ""
    if team_id_or_name is None or isinstance(team_id_or_name, bool):
        return fb
    key = str(team_id_or_name).strip()
    if not key:
        return fb
    resolved = CBA_TEAM_ID_TO_ZH.get(key)
    if resolved:
        return resolved
    by_canonical = _CANONICAL_ID_TO_ZH.get(key.lower())
    if by_canonical:
        return by_canonical
    return fb or key


def format_club_team_display(team_id: object, team_name: object = None) -> Optional[str]:
    """Format a human-readable club display string with honest unknown fallback."""
    id_str = '' if (team_id is None or isinstance(team_id, bool)) else str(team_id).strip()
    name_str = '' if team_name is None else str(team_name).strip()
    resolved_zh = resolve_team_id_zh(id_str)
    if resolved_zh:
        return f'{resolved_zh} (team_id={id_str})'
    if name_str and name_str != id_str:
        return f'{name_str} (team_id={id_str})' if id_str else name_str
    if id_str:
        return f'{id_str} (unresolved_team_id)'
    return None


class UnresolvedClub(ValueError):
    pass


def normalize(name):
    if name is None:
        raise UnresolvedClub('Empty club name')
    text = unicodedata.normalize('NFKC', str(name)).replace('\u3000', ' ')
    text = ' '.join(text.split())
    if not text:
        raise UnresolvedClub('Empty club name')
    return text


class Clubs:
    """Resolve a season club name to a stable club_id, or refuse to guess."""

    def __init__(self, root=None, path=None, strict=True):
        self.path = Path(path) if path else Path(root or '.') / CONFIG
        self.strict = strict
        self.unresolved = []
        self.data = yaml.safe_load(self.path.read_text())
        self.clubs = self.data['clubs']
        self._index = {}
        for club_id, item in self.clubs.items():
            for key in ('official_domestic', 'historical_domestic', 'media'):
                for name in item.get(key) or []:
                    self._index.setdefault((normalize(name), None), club_id)
            for season, names in (item.get('official_foreign_sponsor_by_season') or {}).items():
                for name in names or []:
                    self._index.setdefault((normalize(name), str(season)), club_id)
            for season, names in (item.get('source_typo_variant') or {}).items():
                for name in names or []:
                    self._index.setdefault((normalize(name), str(season)), club_id)

    def resolve(self, name, season=None, role='roster'):
        text = normalize(name)
        season = str(season) if season is not None else None
        club_id = self._index.get((text, season)) or self._index.get((text, None))
        if club_id is None:
            self.unresolved.append({'name': text, 'season': season})
            if self.strict:
                raise UnresolvedClub(f'Unknown club name {text!r} for season {season}')
            return None
        self.allowed(club_id, season, role)
        return club_id

    def allowed(self, club_id, season, role='roster'):
        item = self.clubs[club_id]
        if role != 'roster' or item.get('status') != 'defunct':
            return club_id
        last = item.get('last_roster_season')
        if item.get('post_dissolution_new_roster_forbidden') and last and season and str(season) > str(last):
            raise UnresolvedClub(f'{club_id} is defunct; no new roster after {last}')
        return club_id

    def report(self):
        unique = list({(item['name'], item['season']): item for item in self.unresolved}.values())
        return {'strict': self.strict, 'unresolved': unique,
                'known_clubs': len(self.clubs), 'aliases': len(self._index)}
