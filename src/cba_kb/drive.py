"""Authenticated Drive transport. Credentials stay on this machine."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Dict, Mapping, Optional, Sequence
from .common import atomic, digest
from .drive_http import build_requests_service
from .transport import record_api_call, retry_read

SCOPES = ['https://www.googleapis.com/auth/drive']
FIELDS = 'id,name,mimeType,parents,version,modifiedTime,headRevisionId,md5Checksum,sha256Checksum,size,webViewLink'
MAX_READ_WORKERS = 8
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
_PROTECTED_POLICY_ZONES = ('current', 'history', 'staging', 'evidence')


def _parse_headless_token_info(raw_token: Any) -> Dict[str, Any]:
    """Parse headless OAuth token payload and fail closed on Service Accounts."""
    if isinstance(raw_token, Mapping):
        info: Dict[str, Any] = dict(raw_token)
    elif isinstance(raw_token, (str, bytes, bytearray)):
        text = raw_token.decode('utf-8') if isinstance(raw_token, (bytes, bytearray)) else raw_token
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ValueError('INVALID_DRIVE_TOKEN_JSON') from exc
        if not isinstance(parsed, dict):
            raise ValueError('INVALID_DRIVE_TOKEN_JSON')
        info = parsed
    else:
        raise ValueError('INVALID_DRIVE_TOKEN_JSON')

    cred_type = str(info.get('type') or '').strip().lower()
    client_email = str(info.get('client_email') or '').strip().lower()
    if (
        cred_type == 'service_account'
        or 'private_key' in info
        or 'private_key_id' in info
        or client_email.endswith('.gserviceaccount.com')
    ):
        raise RuntimeError(
            'SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN: personal OAuth authorized_user token required'
        )

    refresh_tok = info.get('refresh_token')
    if not isinstance(refresh_tok, str) or not refresh_tok.strip():
        raise RuntimeError(
            'DRIVE_OAUTH_REFRESH_TOKEN_MISSING: headless token JSON requires refresh_token'
        )
    return info


def credentials(root, interactive=False, credentials_store=None, *, token_json=None, environ=None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from .oauth import authorize

    headless_source = token_json
    if headless_source is None:
        env = os.environ if environ is None else environ
        env_val = env.get('CBA_KB_DRIVE_TOKEN_JSON')
        if isinstance(env_val, str) and env_val.strip():
            headless_source = env_val

    if headless_source is not None:
        info = _parse_headless_token_info(headless_source)
        creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid or not creds.has_scopes(SCOPES):
            raise RuntimeError('Drive headless authorization invalid or missing required scopes')
        return creds

    if credentials_store is None:
        from .instance import load_instance
        credentials_store = load_instance(root).credentials_store
    directory = Path(credentials_store).resolve()
    client, token = directory/'credentials.json', directory/'token.json'
    creds = Credentials.from_authorized_user_file(str(token)) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid or not creds.has_scopes(SCOPES):
        if not interactive:
            raise RuntimeError('Drive authorization missing from Private Instance; run make auth')
        if not client.exists():
            raise RuntimeError('Desktop OAuth client JSON missing from Private Instance credentials store')
        if 'installed' not in json.loads(client.read_text()):
            raise ValueError('Expected Desktop OAuth client (installed)')
        creds = authorize(client, SCOPES)
    directory.mkdir(parents=True,exist_ok=True)
    os.chmod(directory,0o700)
    atomic(token,creds.to_json().encode()); os.chmod(token,0o600)
    return creds


def _close_api_objects(clients):
    for api in clients:
        if api is None:
            continue
        http = getattr(api, '_http', None)
        http = getattr(http, 'http', http)
        if http is not None:
            try:
                http.close()
            except Exception:
                pass


def _assert_main_thread_writer(obj, operation):
    """Enforce 100% single-writer serial execution on the main thread."""
    main_tid = getattr(obj, '_main_thread_id', None)
    if main_tid is not None and threading.get_ident() != main_tid:
        raise RuntimeError(f'WRITE_OFF_MAIN_THREAD_FORBIDDEN:{operation}')


def _read_clients_for(obj):
    getter = getattr(obj, '_get_read_clients', None)
    if callable(getter):
        return getter()
    return getattr(obj, 'api', None), getattr(obj, 'docs', None)


def _reset_hook_for(obj):
    return getattr(obj, '_reset_read_connections', None)


def _validate_max_workers(max_workers):
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not (1 <= max_workers <= MAX_READ_WORKERS):
        raise ValueError(f'max_workers must be an integer between 1 and {MAX_READ_WORKERS}')
    return max_workers


def map_readonly(drive, fn, items, *, max_workers=MAX_READ_WORKERS):
    """Order-preserving bounded read-only concurrency helper for Drive and fakes."""
    workers = _validate_max_workers(max_workers)
    if drive is not None and hasattr(drive, 'map_readonly'):
        return drive.map_readonly(fn, items, max_workers=workers)
    seq = list(items)
    if len(seq) <= 1 or workers == 1:
        return [fn(item) for item in seq]
    sem = threading.BoundedSemaphore(workers)

    def _run(item):
        with sem:
            return fn(item)

    with ThreadPoolExecutor(max_workers=min(workers, len(seq))) as pool:
        futures = [pool.submit(_run, item) for item in seq]
        return [fut.result() for fut in futures]


class Drive:
    def __init__(self, root, instance=None):
        self._credentials = credentials(
            root, credentials_store=instance.credentials_store if instance else None,
        )
        self._main_thread_id = threading.get_ident()
        self._read_local = threading.local()
        self._read_semaphore = threading.BoundedSemaphore(MAX_READ_WORKERS)
        self._worker_clients_lock = threading.Lock()
        self._worker_clients = []
        self.docs = None
        self._build_clients()

    def _build_readonly_client_pair(self):
        """Build an isolated (drive_api, NativeDocs) pair for a worker thread."""
        from .native import NativeDocs
        docs_api = build_requests_service('docs', 'v1', self._credentials)
        drive_api = build_requests_service('drive', 'v3', self._credentials)
        return drive_api, NativeDocs(docs_api)

    def _build_clients(self):
        """Build a fresh main-thread Drive/Docs transport pair from the same credentials."""
        from .native import NativeDocs
        docs_api = build_requests_service('docs', 'v1', self._credentials)
        self.api = build_requests_service('drive', 'v3', self._credentials)
        if self.docs is None:
            self.docs = NativeDocs(docs_api)
        else:
            self.docs.api = docs_api
        self._read_clients = (self.api, docs_api)

    def _is_worker_thread(self):
        main_tid = getattr(self, '_main_thread_id', None)
        return main_tid is not None and threading.get_ident() != main_tid

    def _get_read_clients(self):
        """Return main-thread clients on main thread, or thread-local clients on workers."""
        if not self._is_worker_thread() or getattr(self, '_credentials', None) is None:
            return getattr(self, 'api', None), getattr(self, 'docs', None)
        read_local = getattr(self, '_read_local', None)
        if read_local is None:
            return getattr(self, 'api', None), getattr(self, 'docs', None)
        pair = getattr(read_local, 'clients', None)
        if pair is None:
            pair = self._build_readonly_client_pair()
            read_local.clients = pair
            lock = getattr(self, '_worker_clients_lock', None)
            if lock is not None:
                with lock:
                    self._worker_clients.append(pair)
        return pair

    def _assert_main_thread_writer(self, operation):
        """Enforce 100% single-writer serial execution on the main thread."""
        _assert_main_thread_writer(self, operation)

    def _reset_read_connections(self):
        """Recreate fresh read transports before a read retry, never a write.

        On a worker thread, only that thread's local read client is closed and
        rebuilt so the main thread and sibling workers are never disrupted.
        """
        if self._is_worker_thread():
            read_local = getattr(self, '_read_local', None)
            pair = getattr(read_local, 'clients', None) if read_local is not None else None
            if pair is not None:
                drive_api, docs_adapter = pair
                _close_api_objects((drive_api, getattr(docs_adapter, 'api', None)))
                read_local.clients = None
            if getattr(self, '_credentials', None) is None or read_local is None:
                return
            try:
                new_pair = self._build_readonly_client_pair()
                read_local.clients = new_pair
                lock = getattr(self, '_worker_clients_lock', None)
                if lock is not None:
                    with lock:
                        self._worker_clients.append(new_pair)
            except Exception:
                pass
            return

        clients = getattr(self, '_read_clients', None) or (getattr(self, 'api', None),)
        _close_api_objects(clients)
        if getattr(self, '_credentials', None) is None:
            return
        try:
            self._build_clients()
        except Exception:
            # Keep the closed transport: the next attempt fails closed instead
            # of silently reusing a known-broken session.
            pass

    def _close_worker_read_clients(self):
        lock = getattr(self, '_worker_clients_lock', None)
        if lock is None:
            return
        with lock:
            pairs = list(self._worker_clients)
            self._worker_clients.clear()
        for drive_api, docs_adapter in pairs:
            _close_api_objects((drive_api, getattr(docs_adapter, 'api', None)))

    def map_readonly(self, fn, items, *, max_workers=MAX_READ_WORKERS):
        """Execute a read-only callable across items in bounded concurrency, preserving order."""
        workers = _validate_max_workers(max_workers)
        seq = list(items)
        if len(seq) <= 1 or workers == 1:
            return [fn(item) for item in seq]
        sem = getattr(self, '_read_semaphore', None) or threading.BoundedSemaphore(MAX_READ_WORKERS)
        local_sem = threading.BoundedSemaphore(workers) if workers < MAX_READ_WORKERS else None

        def _run(item):
            with sem:
                if local_sem is not None:
                    with local_sem:
                        return fn(item)
                return fn(item)

        try:
            with ThreadPoolExecutor(max_workers=min(workers, len(seq))) as pool:
                futures = [pool.submit(_run, item) for item in seq]
                return [fut.result() for fut in futures]
        finally:
            self._close_worker_read_clients()

    def document_json(self, file_id):
        """Raw Docs readback under the bounded read-retry policy."""
        def _call():
            _, docs = _read_clients_for(self)
            record_api_call('download_calls')
            return docs.document(file_id)
        return retry_read(_call, label='native document',
                          reset=_reset_hook_for(self))

    def get_managed_doc(self,file_id):
        if self.meta(file_id)['mimeType']!='application/vnd.google-apps.document':
            raise ValueError('Not a native Doc')
        def _call():
            _, docs = _read_clients_for(self)
            record_api_call('download_calls')
            return docs.get(file_id)
        return retry_read(_call, label='native doc',
                          reset=_reset_hook_for(self))

    def put_managed_doc(self,file_id,content):
        _assert_main_thread_writer(self, 'put_managed_doc')
        if self.meta(file_id)['mimeType']!='application/vnd.google-apps.document':
            raise ValueError('Not a native Doc')
        record_api_call('upload_calls')
        return self.docs.put(file_id,content)

    def index_cba_keys(self, parent):
        """List all pages once; reject ambiguous identities before any writes."""
        index = {}
        for item in self.list(parent):
            key = item.get('appProperties', {}).get('cba_key')
            if key is None:
                continue
            if key in index:
                raise RuntimeError('Duplicate release object key')
            index[key] = item
        return index

    def ensure_copy(self,parent,key,file_id,name,*,index=None):
        _assert_main_thread_writer(self, 'ensure_copy')
        index = self.index_cba_keys(parent) if index is None else index
        source_mime = self.meta(file_id)['mimeType']
        if key in index:
            existing = index[key]
            if existing['mimeType'] != source_mime or parent not in existing.get('parents', []):
                raise RuntimeError('Existing backup copy MIME/parent mismatch')
            return existing['id']
        record_api_call('copy_calls')
        result = self.api.files().copy(fileId=file_id,body={'name':name,'parents':[parent],
            'appProperties':{'cba_key':key}},fields='id',supportsAllDrives=True).execute(num_retries=0)['id']
        index[key] = {'id': result, 'mimeType': source_mime, 'parents': [parent],
                      'appProperties': {'cba_key': key}}
        return result

    def meta(self, file_id):
        def _call():
            api, _ = _read_clients_for(self)
            record_api_call('meta_calls')
            return api.files().get(fileId=file_id,fields=FIELDS,
                supportsAllDrives=True).execute(num_retries=0)
        return retry_read(_call, label='meta',
            reset=_reset_hook_for(self))

    def get(self, file_id):
        meta = self.meta(file_id)
        if meta['mimeType'].startswith('application/vnd.google-apps.'):
            raise ValueError('Native object requires native adapter; raw download rejected')
        return self._download(file_id)

    def get_with_meta(self, file_id, *, known_meta=None):
        """Download binary bytes once, reusing known_meta & server sha256Checksum when valid."""
        before = known_meta if known_meta is not None else self.meta(file_id)
        if before['mimeType'].startswith('application/vnd.google-apps.'):
            raise ValueError('Native object requires native adapter; raw download rejected')
        data = self._download(file_id)
        server_sha = before.get('sha256Checksum')
        if isinstance(server_sha, str) and _HEX64_RE.fullmatch(server_sha.lower()):
            if digest(data) != server_sha.lower():
                raise RuntimeError('Remote changed during download')
            return data, before
        after = self.meta(file_id)
        fp_keys = ('id', 'version', 'modifiedTime', 'mimeType', 'parents')
        if {k: before.get(k) for k in fp_keys} != {k: after.get(k) for k in fp_keys}:
            raise RuntimeError('Remote changed during download')
        return data, after

    def get_bounded(self, file_id, max_bytes):
        """Download with a hard byte bound enforced during transfer.

        Reads in chunks and aborts as soon as a completed chunk pushes the
        accumulated bytes over ``max_bytes`` (raising
        ``DownloadTooLargeError``), so a lying or missing ``size`` in
        metadata can never cause unbounded buffering. The declared size is
        used only as an early-reject optimization, never as the safety
        proof. Transient chunk failures are retried under the standard
        read-retry policy; the bound is enforced on every attempt.

        Residual: the bound is checked after each chunk lands, so the
        in-memory buffer may transiently hold up to ``max_bytes`` plus one
        chunk (chunksize) before the abort fires. Over-limit bytes are never
        checksummed, verified, or returned as success.
        """
        from .transport import DownloadTooLargeError
        if max_bytes <= 0:
            raise ValueError('max_bytes must be positive')
        meta = self.meta(file_id)
        if meta['mimeType'].startswith('application/vnd.google-apps.'):
            raise ValueError('Native object requires native adapter; raw download rejected')
        try:
            declared = int(meta.get('size') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            raise DownloadTooLargeError(
                f'declared size {declared} exceeds max_bytes={max_bytes}')
        from googleapiclient.http import MediaIoBaseDownload
        def bounded():
            # Fresh buffer + downloader per attempt: a retry after a
            # transport reset must not reuse a downloader bound to the
            # closed session (same structure as _download).
            api, _ = _read_clients_for(self)
            record_api_call('download_calls')
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buf, api.files().get_media(fileId=file_id,
                                           supportsAllDrives=True),
                chunksize=min(1024 * 1024, max_bytes))
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=0)
                if buf.tell() > max_bytes:
                    raise DownloadTooLargeError(
                        f'download exceeded max_bytes={max_bytes}')
            return buf.getvalue()
        data = retry_read(bounded, label='bounded download',
                          reset=_reset_hook_for(self))
        if len(data) > max_bytes:
            # Defense in depth: the transport must never return more than asked.
            raise DownloadTooLargeError(
                f'download returned {len(data)} bytes over max_bytes={max_bytes}')
        return data

    def _download(self, file_id):
        """Raw content read after MIME has been checked by get or the live index."""
        from googleapiclient.http import MediaIoBaseDownload
        def download():
            api, _ = _read_clients_for(self)
            record_api_call('download_calls')
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf,api.files().get_media(fileId=file_id,supportsAllDrives=True))
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=0)
            return buf.getvalue()
        return retry_read(download, label='download', reset=_reset_hook_for(self))

    def put(self, file_id, content, mime):
        _assert_main_thread_writer(self, 'put')
        from googleapiclient.http import MediaIoBaseUpload
        actual = self.meta(file_id)['mimeType']
        if actual != mime or actual.startswith('application/vnd.google-apps.'):
            raise ValueError('MIME mismatch/native byte update rejected')
        record_api_call('upload_calls')
        return self.api.files().update(fileId=file_id,media_body=MediaIoBaseUpload(io.BytesIO(content),mimetype=mime),
                                      fields=FIELDS,supportsAllDrives=True).execute(num_retries=0)

    def move(self,file_id,destination,previous):
        _assert_main_thread_writer(self, 'move')
        record_api_call('move_calls')
        return self.api.files().update(fileId=file_id,addParents=destination,removeParents=previous,
            fields=FIELDS,supportsAllDrives=True).execute(num_retries=0)

    def list(self, parent):
        items, page = [], None
        while True:
            def _page(token=page):
                api, _ = _read_clients_for(self)
                record_api_call('list_calls')
                return api.files().list(
                    q=f"'{parent}' in parents and trashed=false",pageToken=token,
                    pageSize=1000,fields=f'nextPageToken,files({FIELDS},appProperties)',supportsAllDrives=True,
                    includeItemsFromAllDrives=True).execute(num_retries=0)
            result = retry_read(_page, label='list',
                reset=_reset_hook_for(self))
            items.extend(result.get('files',[])); page=result.get('nextPageToken')
            if not page: return items

    def ensure(self, parent, key, name, mime, content=None, *, index=None):
        _assert_main_thread_writer(self, 'ensure')
        # Recovery after a successful create with a lost response: same key, no blind duplicate.
        index = self.index_cba_keys(parent) if index is None else index
        if key in index:
            result=index[key]
            if result['mimeType']!=mime or parent not in result.get('parents', []):
                raise ValueError('Existing object MIME/parent mismatch')
            if content is not None:
                if mime.startswith('application/vnd.google-apps.'):
                    raise ValueError('Native object requires native adapter; raw download rejected')
                if digest(self._download(result['id']))!=digest(content):
                    raise RuntimeError('Immutable release object differs')
            return result['id']
        from googleapiclient.http import MediaIoBaseUpload
        body={'name':name,'parents':[parent],'mimeType':mime,'appProperties':{'cba_key':key}}
        kwargs={'body':body,'fields':'id','supportsAllDrives':True}
        if content is not None: kwargs['media_body']=MediaIoBaseUpload(io.BytesIO(content),mimetype=mime)
        record_api_call('upload_calls')
        result = self.api.files().create(**kwargs).execute(num_retries=0)['id']
        index[key] = {'id': result, 'mimeType': mime, 'parents': [parent],
                      'appProperties': {'cba_key': key}}
        return result

    def create_binary(self, parent, name, content, mime):
        _assert_main_thread_writer(self, 'create_binary')
        from googleapiclient.http import MediaIoBaseUpload
        body = {'name': name, 'parents': [parent], 'mimeType': mime}
        record_api_call('upload_calls')
        return self.api.files().create(
            body=body,
            media_body=MediaIoBaseUpload(io.BytesIO(content), mimetype=mime),
            fields=FIELDS,
            supportsAllDrives=True,
        ).execute(num_retries=0)


def _collect_protected_folder_ids(
    forbidden_folder_ids: Sequence[str] = (),
    production_policy: Optional[Mapping[str, Any]] = None,
) -> set[str]:
    protected: set[str] = set()
    for fid in forbidden_folder_ids or ():
        if isinstance(fid, str) and fid.strip():
            protected.add(fid.strip())
    if isinstance(production_policy, Mapping):
        root_id = production_policy.get('root_folder_id')
        if isinstance(root_id, str) and root_id.strip():
            protected.add(root_id.strip())
        zones = production_policy.get('zones')
        if isinstance(zones, Mapping):
            for zone_key in (*_PROTECTED_POLICY_ZONES, 'root', 'root_folder_id'):
                val = zones.get(zone_key)
                if isinstance(val, str) and val.strip():
                    protected.add(val.strip())
                elif isinstance(val, (list, tuple, set)):
                    for item in val:
                        if isinstance(item, str) and item.strip():
                            protected.add(item.strip())
    return protected


def upsert_snowball_card_to_drive(
    drive,
    *,
    folder_id: str,
    filename: str,
    content_bytes: bytes,
    forbidden_folder_ids: Sequence[str] = (),
    production_policy: Optional[Mapping[str, Any]] = None,
    mime_type: str = 'text/markdown',
) -> Dict[str, Any]:
    """Topology-safe single-card Drive upserter for isolated snowball folder."""
    _assert_main_thread_writer(drive, 'upsert_snowball_card_to_drive')

    if not isinstance(folder_id, str) or not folder_id.strip():
        raise ValueError('SNOWBALL_FOLDER_ID_REQUIRED')
    target_folder_id = folder_id.strip()

    if (
        not isinstance(filename, str)
        or not filename.strip()
        or '/' in filename
        or '\\' in filename
    ):
        raise ValueError('SNOWBALL_FILENAME_INVALID')
    clean_filename = filename.strip()

    if not isinstance(content_bytes, (bytes, bytearray)):
        raise TypeError('SNOWBALL_CONTENT_MUST_BE_BYTES')
    payload = bytes(content_bytes)

    protected_ids = _collect_protected_folder_ids(
        forbidden_folder_ids=forbidden_folder_ids,
        production_policy=production_policy,
    )
    if target_folder_id in protected_ids:
        raise ValueError('SNOWBALL_FOLDER_IN_PROTECTED_ZONE')

    from .current_state import clean
    clean(payload, clean_filename)

    existing_matches = [
        item
        for item in drive.list(target_folder_id)
        if isinstance(item, Mapping) and item.get('name') == clean_filename
    ]
    if len(existing_matches) > 1:
        raise RuntimeError(f'SNOWBALL_DUPLICATE_FILENAME:{clean_filename}')

    if existing_matches:
        drive.put(existing_matches[0]['id'], payload, mime_type)
        action = 'updated'
    else:
        if hasattr(drive, 'create_binary'):
            drive.create_binary(target_folder_id, clean_filename, payload, mime_type)
        else:
            drive.ensure(
                target_folder_id,
                f'snowball/{clean_filename}',
                clean_filename,
                mime_type,
                payload,
            )
        action = 'created'

    return {
        'action': action,
        'filename': clean_filename,
        'sha256': digest(payload),
        'size_bytes': len(payload),
        'folder_role': 'SNOWBALL_ON_DEMAND',
        'drive_id_redacted': True,
    }
