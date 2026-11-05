"""One scanner for Git blobs, publish preflight and readback.

Findings never contain matched content. Office archives are inspected in memory
with bounds; an unreadable, encrypted or oversized archive fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile
from xml.etree import ElementTree

KEY = r'(?:api[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token|id[_-]?token|private[_-]?key|password|passwd|session(?:[_-]?(?:token|id|cookie))?|cookie)'
ASSIGNMENT = re.compile(
    r'(?i)(?<![\w])([\w.-]*' + KEY + r')[\"\']?[ \t]*[:=]'
    r'(?:\s*\"([^\"\r\n]*)\"|\s*\'([^\'\r\n]*)\'|[ \t]*(\$\{[A-Z_][A-Z0-9_]*\}|\$\{\{\s*[^}]+\s*\}\}|[^\s,;\}\]\r\n]+))'
)
PATTERNS = {
    'S_PRIVATE_KEY': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'),
    'S_BEARER': re.compile(r'(?i)\bBearer\s+([A-Za-z0-9_./+~=-]+)'),
    'S_PROVIDER_TOKEN': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b'),
    'S_URL_CREDENTIAL': re.compile(r'https?://[^\s/@:]+:[^\s/@]+@'),
}
REFERENCE = re.compile(r'(?:\$\{[A-Z_][A-Z0-9_]*\}|\$[A-Z_][A-Z0-9_]*|<[A-Z_][A-Z0-9_]*>|\{\{[^}]+\}\}|\$\{\{\s*[^}]+\s*\}\})\Z')
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 2000

# Drive-ID morphology, REQ-212 Stage 2 formal (pattern set v1 semantics, frozen
# census definition). Findings carry a SHA-256 fingerprint of the matched
# token only -- never the token itself, never matched content. The URL and
# field forms follow the census definition exactly; the hex/digit exclusions
# below apply to the standalone form only, as in the census. A morphology
# finding is NOT a verdict that a value is a real private ID: synthetic
# values with the same shape match too, which is why the gate is fail-closed
# and disposition (AC-B3) is a separate, evidence-bound step. The patterns
# are written with escaped separators so this file's own source text does
# not itself match them. Residual risks (not closed by this gate): the frozen
# pattern set v1 itself misses some forms (e.g. /drive/folders/<id> whose id
# does not start with '1' — a pattern-version question, not a scanner
# deviation); a digest verifies bytes, not path binding.
_M_DRIVE_HOST = r'drive\.google\.com'
M_DRIVE_URL = re.compile(
    rb'https?://' + _M_DRIVE_HOST.encode() +
    rb'/(?:drive/(?:folders|file/d/)|file/d/|open\?id=|uc\?(?:export=download&)?id=)'
    rb'([A-Za-z0-9_-]{10,})')
M_DRIVE_FIELD = re.compile(
    rb'["\'](?:id|fileId|file_id|folderId|parentId)["\']\s*:\s*["\']'
    rb'(1[A-Za-z0-9_-]{10,})["\']')
M_DRIVE_TOKEN = re.compile(rb'(?<![A-Za-z0-9_-])1[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])')
M_HEX40 = re.compile(rb'^[0-9a-f]{40}$')
M_HEX64 = re.compile(rb'^[0-9a-f]{64}$')
M_DIGITS = re.compile(rb'^\d+$')
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')


def _placeholder(value):
    return (not value or value in {'null', 'None', 'true', 'false', '[REDACTED]'}
            or bool(REFERENCE.fullmatch(value))
            or value.startswith(('os.environ[', 'os.getenv(', 'environ.get(', 'getenv(')))


def _morphology_in_path(path):
    """True if any '!' component of path itself matches Drive-ID morphology.

    A ZIP member name or caller path that IS an identifier-shaped token must
    not survive into finding output verbatim.
    """
    for component in str(path).split('!'):
        data = component.encode('utf-8')
        if M_DRIVE_URL.search(data) or M_DRIVE_FIELD.search(data):
            return True
        for match in M_DRIVE_TOKEN.finditer(data):
            token = match.group(0)
            if not (M_HEX40.match(token) or M_HEX64.match(token) or M_DIGITS.match(token)):
                return True
    return False


def _safe_path(path):
    """Path for finding output: fingerprinted when it itself carries morphology."""
    text = str(path)
    if _morphology_in_path(text):
        return '[path-sha256:' + hashlib.sha256(text.encode()).hexdigest()[:12] + ']'
    return text


def _finding(path, rule, value=b'', line=0):
    if isinstance(value, str):
        value = value.encode('utf-8')
    if (any(pattern.search(str(path)) for pattern in PATTERNS.values())
            or ASSIGNMENT.search(str(path)) or _morphology_in_path(path)):
        path = '[path-sha256:' + hashlib.sha256(str(path).encode()).hexdigest()[:12] + ']'
    return {'path': str(path), 'rule_id': rule, 'line': line,
            'fingerprint': 'sha256:' + hashlib.sha256(value).hexdigest()[:12]}


def _morphology_tokens(text):
    """Yield matched token bytes under pattern set v1 semantics (no content out).

    The census patterns are byte patterns; the text scanner works on decoded
    text, so encode back to bytes here. Line numbers count b'\\n' bytes, which
    is exact because tokens are ASCII and newlines are single bytes.
    """
    encoded = text.encode('utf-8')
    seen = set()
    for match in M_DRIVE_URL.finditer(encoded):
        token = match.group(1)
        if token not in seen:
            seen.add(token)
            yield token, encoded.count(b'\n', 0, match.start()) + 1
    for match in M_DRIVE_FIELD.finditer(encoded):
        token = match.group(1)
        if token not in seen:
            seen.add(token)
            yield token, encoded.count(b'\n', 0, match.start()) + 1
    for match in M_DRIVE_TOKEN.finditer(encoded):
        token = match.group(0)
        if M_HEX40.match(token) or M_HEX64.match(token) or M_DIGITS.match(token):
            continue  # Git hashes and pure-digit tokens are not Drive-ID morphology.
        if token not in seen:
            seen.add(token)
            yield token, encoded.count(b'\n', 0, match.start()) + 1


def _scan_text(text, path, *, morphology=False):
    findings = []
    for match in ASSIGNMENT.finditer(text):
        value = next(group for group in match.groups()[1:] if group is not None)
        if (str(path).endswith('.py') and match.group(4) is not None
                and re.match(r'[A-Za-z_]\w*(?:\.\w+)*(?:\(|\[)', value)):
            continue  # Python computation/reference, not a literal credential value.
        if not _placeholder(value):
            findings.append(_finding(path, 'S_SECRET_VALUE', value, text.count('\n', 0, match.start()) + 1))
    for rule, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if rule == 'S_BEARER' and _placeholder(match.group(1)):
                continue
            findings.append(_finding(path, rule, match.group(), text.count('\n', 0, match.start()) + 1))
    if morphology:
        for token, line in _morphology_tokens(text):
            findings.append(_finding(path, 'S_DRIVE_ID_MORPHOLOGY', token, line))
    return findings


def scan_bytes(data, path='<payload>', *, _depth=0, _budget=None, morphology=False):
    """Return redacted findings for exact bytes, including zipped Office XML.

    morphology=True additionally reports Drive-ID morphology findings
    (rule S_DRIVE_ID_MORPHOLOGY, fingerprint only). Default False preserves
    the pre-existing secret-scan contract exactly.
    """
    if not isinstance(data, bytes):
        raise TypeError('Secret scanner requires bytes')
    budget = _budget if _budget is not None else [MAX_ARCHIVE_BYTES, MAX_MEMBERS]
    member_name = str(path).rsplit('!', 1)[-1].replace('\\', '/')
    name = PurePosixPath(member_name).name.lower()
    parts = PurePosixPath(member_name).parts
    findings = []
    if ('.credentials' in parts or name in {'credentials.json', 'token.json', '.env'}
            or name.startswith(('client_secret', 'service-account', 'service_account'))
            or '_private_key' in name
            or (name.startswith('.env.') and name not in {'.env.example', '.env.template'})):
        findings.append(_finding(path, 'S_CREDENTIAL_FILE', data))
    is_zip = data.startswith(b'PK') or name.endswith(('.zip', '.xlsx', '.docx', '.pptx', '.jar', '.odt', '.ods'))
    if is_zip:
        if _depth >= 3:
            return findings + [_finding(path, 'S_ARCHIVE_LIMIT', data)]
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > budget[1] or sum(m.file_size for m in members) > budget[0]:
                    return findings + [_finding(path, 'S_ARCHIVE_LIMIT', data)]
                budget[1] -= len(members)
                for member in members:
                    if member.is_dir():
                        continue
                    if member.flag_bits & 1:
                        return findings + [_finding(path, 'S_ARCHIVE_UNREADABLE', data)]
                    budget[0] -= member.file_size
                    content = archive.read(member)
                    member_path = str(path) + '!' + member.filename
                    findings.extend(scan_bytes(content, member_path, _depth=_depth + 1, _budget=budget, morphology=morphology))
                    if member.filename.endswith('.xml'):
                        try:
                            fragments = list(ElementTree.fromstring(content).itertext())
                            for separator in ('', ' '):
                                findings.extend(_scan_text(separator.join(fragments), member_path, morphology=morphology))
                        except ElementTree.ParseError:
                            findings.append(_finding(member_path, 'S_ARCHIVE_UNREADABLE', content))
        except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, NotImplementedError):
            findings.append(_finding(path, 'S_ARCHIVE_UNREADABLE', data))
        return findings
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    text = data.decode(encoding, errors='replace')
    findings.extend(_scan_text(text, path, morphology=morphology))
    if morphology and encoding == 'utf-16':
        # The BOM-indicated decoding can mangle the input (e.g. BOM-prefixed
        # UTF-8 bytes decoded as UTF-16): a morphology token contiguous in
        # the raw bytes would otherwise escape, while the frozen census
        # (raw-byte patterns) still catches it. Scan the UTF-8 view as well,
        # reporting only tokens not already found (fingerprint dedup).
        # Genuine UTF-16 content cannot false-positive here: its raw bytes
        # never contain a contiguous ASCII token run.
        seen = {f['fingerprint'] for f in findings if f['rule_id'] == 'S_DRIVE_ID_MORPHOLOGY'}
        for token, line in _morphology_tokens(data.decode('utf-8-sig', errors='replace')):
            finding = _finding(path, 'S_DRIVE_ID_MORPHOLOGY', token, line)
            if finding['fingerprint'] not in seen:
                seen.add(finding['fingerprint'])
                findings.append(finding)
    if name.endswith('.json') or text.lstrip().startswith(('{', '[')):
        try:
            decoded = json.loads(text)
            normalized = json.dumps(decoded, ensure_ascii=False)
            if normalized != text:
                findings.extend(_scan_text(normalized, path, morphology=morphology))
        except (ValueError, RecursionError):
            pass  # Free text is still scanned above.
    return findings


def scan_morphology_bytes(data, path='<payload>', *, include_infrastructure=True):
    """Return Drive-ID morphology findings for exact bytes (fingerprint only).

    With include_infrastructure (default), archive-unreadable/limit findings
    are included, not filtered out: an unreadable archive must fail the
    morphology guard instead of passing silently on "no morphology found".
    Callers that need the pure morphology predicate (reintroduction check)
    pass include_infrastructure=False.
    """
    if not isinstance(data, bytes):
        raise TypeError('Morphology scanner requires bytes')
    wanted = {'S_DRIVE_ID_MORPHOLOGY'}
    if include_infrastructure:
        wanted |= {'S_ARCHIVE_UNREADABLE', 'S_ARCHIVE_LIMIT'}
    return [f for f in scan_bytes(data, path, morphology=True) if f['rule_id'] in wanted]


def require_morphology_clean(data, path='<payload>'):
    findings = scan_morphology_bytes(data, path)
    if findings:
        raise RuntimeError('MORPHOLOGY_GUARD: ' + json.dumps(findings, ensure_ascii=True, sort_keys=True))


def verify_digest(path, expected_sha256):
    """Fail-closed digest check for a release artifact or manifest.

    Returns (ok, reason) with reason in OK / MISSING_ARTIFACT /
    UNREADABLE_ARTIFACT / MALFORMED_DIGEST / DIGEST_MISMATCH. A substituted
    reference fails here because the bytes at the referenced path no longer
    match the recorded digest. Never raises for the checked conditions.

    Residual risk: this compares the bytes at a path with a digest; it does
    not authenticate the path binding. A path and digest replaced together
    still verify. Callers must bind the expected digest to a trusted
    reference outside this check.
    """
    if not isinstance(expected_sha256, str) or not _DIGEST_RE.match(expected_sha256):
        return (False, 'MALFORMED_DIGEST')
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return (False, 'MISSING_ARTIFACT')
    except OSError:
        return (False, 'UNREADABLE_ARTIFACT')
    actual = hashlib.sha256(data).hexdigest()
    return (actual == expected_sha256, 'OK' if actual == expected_sha256 else 'DIGEST_MISMATCH')


def _git_blob_id(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def historical_blob_ids(root='.'):
    """Set of blob SHA-1s reachable from any ref. Raises on git failure (fail closed).

    A shallow repository hides history, so it raises HISTORY_UNAVAILABLE
    instead of reporting "no re-introduction" against a truncated object set.
    Unexpected or missing objects in batch-check output raise as well.
    """
    try:
        shallow = subprocess.check_output(
            ['git', 'rev-parse', '--is-shallow-repository'],
            cwd=root, stderr=subprocess.STDOUT).decode().strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError('HISTORY_UNAVAILABLE: ' + str(exc))
    if shallow == 'true':
        raise RuntimeError('HISTORY_UNAVAILABLE: shallow repository hides history')
    try:
        listing = subprocess.check_output(
            ['git', 'rev-list', '--objects', '--all'], cwd=root, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError('HISTORY_UNAVAILABLE: ' + str(exc))
    oids = [line.split()[0] for line in listing.decode().splitlines() if line.split()]
    if not oids:
        # No reachable history to compare against: fail closed rather than
        # report "no re-introduction" on an unreadable history.
        raise RuntimeError('HISTORY_UNAVAILABLE: no reachable objects')
    proc = subprocess.run(
        ['git', 'cat-file', '--batch-check=%(objectname) %(objecttype)'],
        input='\n'.join(oids), capture_output=True, text=True, cwd=root)
    if proc.returncode != 0:
        raise RuntimeError('HISTORY_UNAVAILABLE: ' + proc.stderr.strip())
    blobs = set()
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise RuntimeError('HISTORY_UNAVAILABLE: unexpected batch-check line')
        if fields[1] == 'missing':
            raise RuntimeError('HISTORY_UNAVAILABLE: missing object in history')
        if fields[1] == 'blob':
            blobs.add(fields[0])
    return blobs


def reintroduced_findings(root='.', *, staged=True):
    """Flag staged blobs byte-identical to a historical blob carrying morphology tokens.

    A token-level re-introduction is already caught by the morphology gate on
    staged files (any morphology token in new/changed material fails); this
    check adds the exact historical-file case AC-B2 names: a whole historical
    file re-introduced verbatim. Git failures raise RuntimeError so callers
    fail closed instead of passing on an unreadable history.
    """
    command = (['diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if staged
               else ['ls-tree', '-r', '--name-only', '-z', 'HEAD'])
    try:
        paths = subprocess.check_output(['git', *command], cwd=root).decode().split('\0')
        history = historical_blob_ids(root)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError('HISTORY_UNAVAILABLE: ' + str(exc))
    findings = []
    for path in filter(None, paths):
        ref = ':' + path if staged else 'HEAD:' + path
        try:
            data = subprocess.check_output(['git', 'show', ref], cwd=root)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError('HISTORY_UNAVAILABLE: ' + str(exc))
        if _git_blob_id(data) in history and scan_morphology_bytes(data, path, include_infrastructure=False):
            findings.append({'path': _safe_path(path), 'rule_id': 'S_REINTRODUCED_HISTORICAL',
                             'line': 0,
                             'fingerprint': 'sha256:' + hashlib.sha256(data).hexdigest()[:12]})
    return findings


def require_clean(data, path='<payload>'):
    findings = scan_bytes(data, path)
    if findings:
        raise RuntimeError('SECRET_GUARD: ' + json.dumps(findings, ensure_ascii=True, sort_keys=True))


def scan_git(root='.', *, staged=True, morphology=False):
    """Scan index blobs, not working-tree substitutes; tracked mode scans HEAD."""
    command = (['diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if staged
               else ['ls-tree', '-r', '--name-only', '-z', 'HEAD'])
    paths = subprocess.check_output(['git', *command], cwd=root).decode().split('\0')
    findings = []
    for path in filter(None, paths):
        ref = ':' + path if staged else 'HEAD:' + path
        data = subprocess.check_output(['git', 'show', ref], cwd=root)
        findings.extend(scan_bytes(data, path, morphology=morphology))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tracked', action='store_true', help='Scan every HEAD blob (CI)')
    parser.add_argument('--morphology', action='store_true',
                        help='Also report Drive-ID morphology findings (REQ-212 PR gate; '
                             'tracked mode on current HEAD is diagnostic: pre-existing '
                             'findings belong to the AC-B3 disposition record, not to a '
                             'retroactive pass/fail on untouched history)')
    parser.add_argument('--reintroduction', action='store_true',
                        help='Also flag staged blobs byte-identical to a historical blob '
                             'carrying morphology tokens (fail closed on unreadable history)')
    parser.add_argument('--digest', nargs=2, metavar=('PATH', 'SHA256'),
                        help='Verify one artifact digest; exit 0 on match, 2 otherwise')
    parser.add_argument('paths', nargs='*', type=Path, help='Explicit candidate/readback files')
    args = parser.parse_args(argv)
    if args.digest and (args.morphology or args.reintroduction or args.tracked or args.paths):
        # Digest verification and scanning are separate runs: a --digest
        # success previously returned before any scan ran, so combined flags
        # could silently skip scanning. Reject the combination instead.
        print(json.dumps({'status': 'FAIL', 'reason': 'DIGEST_MODE_EXCLUSIVE',
                          'detail': '--digest cannot be combined with scanning inputs'},
                         sort_keys=True), file=sys.stderr)
        return 2
    if args.digest:
        ok, reason = verify_digest(args.digest[0], args.digest[1])
        if ok:
            print('Digest verified')
            return 0
        print(json.dumps({'status': 'FAIL', 'reason': reason,
                          'path': _safe_path(args.digest[0])}, sort_keys=True), file=sys.stderr)
        return 2
    findings = []
    infrastructure_failure = False
    if args.paths:
        for path in args.paths:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                findings.append(_finding(str(path), 'S_MISSING_ARTIFACT', str(path)))
                infrastructure_failure = True
                continue
            except OSError:
                findings.append(_finding(str(path), 'S_UNREADABLE_ARTIFACT', str(path)))
                infrastructure_failure = True
                continue
            findings.extend(scan_bytes(data, str(path), morphology=args.morphology))
    else:
        findings = scan_git(staged=not args.tracked, morphology=args.morphology)
    if args.reintroduction:
        try:
            findings.extend(reintroduced_findings(staged=True))
        except RuntimeError as exc:
            findings.append({'path': '<history>', 'rule_id': 'S_HISTORY_UNAVAILABLE',
                             'line': 0, 'fingerprint': 'sha256:' + hashlib.sha256(str(exc).encode()).hexdigest()[:12]})
            infrastructure_failure = True
    if findings:
        print(json.dumps({'status': 'FAIL', 'findings': findings}, sort_keys=True), file=sys.stderr)
        return 2 if infrastructure_failure else 1
    print('Secret scan passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
