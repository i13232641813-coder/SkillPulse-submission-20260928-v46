"""私有 Skill 上传待审区：只做静态读取，不导入或运行上传的代码。"""
import io
import hashlib
import json
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from governance.checks import check_documentation, check_static_scan
from governance.schema import normalize_skill_meta, validate_frontmatter

MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_FILES = 100
SKILL_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
FORBIDDEN_NAMES = {'.env', '.env.local', 'id_rsa', 'id_ed25519', 'credentials.json', 'secrets.json'}
REVIEW_DECISIONS = {'PRELIMINARY_PASS', 'CHANGES_REQUESTED', 'REJECTED'}
TEXT_SUFFIXES = {'.py', '.md', '.json', '.yaml', '.yml', '.sh', '.txt', '.ps1', '.cmd', '.bat'}


class PackageError(ValueError):
    pass


def _standard_skill_summary(skill_dir: Path) -> str | None:
    """只识别标准 SKILL.md 的身份字段；不推断运行端口或依赖。"""
    path = skill_dir / 'SKILL.md'
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        return None
    text = path.read_text(encoding='utf-8', errors='replace')
    if not text.startswith('---\n'):
        return None
    header = text.split('---', 2)
    if len(header) < 3:
        return None
    values = {}
    for line in header[1].splitlines():
        if ':' in line and not line.startswith((' ', '\t')):
            key, value = line.split(':', 1)
            values[key.strip()] = value.strip().strip('"\'')
    if values.get('name') != skill_dir.name or not values.get('description'):
        return None
    return values['description'][:240]


def _content_digest(skill_dir: Path) -> str:
    """对解压后的相对路径和文件内容建立稳定摘要，不把 ZIP 时间戳当内容。"""
    digest = hashlib.sha256()
    for path in sorted(skill_dir.rglob('*')):
        if path.is_symlink():
            raise PackageError('隔离目录包含符号链接')
        if path.is_file():
            relative = path.relative_to(skill_dir).as_posix().encode('utf-8')
            digest.update(len(relative).to_bytes(4, 'big'))
            digest.update(relative)
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _members(archive: zipfile.ZipFile):
    entries = archive.infolist()
    files = [item for item in entries if not item.is_dir()]
    if not files or len(files) > MAX_FILES:
        raise PackageError('ZIP 必须包含 1–100 个文件')
    if sum(item.file_size for item in files) > MAX_EXPANDED_BYTES:
        raise PackageError('解压后文件总量超过 20 MiB')
    roots, seen = set(), set()
    for item in entries:
        name = item.filename
        path = PurePosixPath(name)
        parts = path.parts
        if not parts or name.startswith('/') or '\\' in name or ':' in name or '.' in parts or '..' in parts:
            raise PackageError('ZIP 含不安全路径')
        if len(parts) < 2 or not SKILL_NAME.fullmatch(parts[0]):
            raise PackageError('ZIP 须只有一个以 Skill 名称命名的顶层目录')
        if name.casefold() in seen:
            raise PackageError('ZIP 包含重复文件名')
        seen.add(name.casefold())
        roots.add(parts[0])
        if stat.S_ISLNK(item.external_attr >> 16):
            raise PackageError('ZIP 不允许符号链接')
        if any(part.lower() in FORBIDDEN_NAMES for part in parts):
            raise PackageError('ZIP 包含疑似密钥文件，已拒绝')
    if len(roots) != 1:
        raise PackageError('ZIP 只能包含一个 Skill 目录')
    return entries, roots.pop()


def static_report(skill_dir: Path) -> dict:
    meta = None
    standard_summary = None
    try:
        meta = normalize_skill_meta(skill_dir)
        issues = validate_frontmatter(skill_dir, meta)
        catalog_ok, catalog_detail = not issues, '; '.join(issues) if issues else 'Schema 与 frontmatter 一致'
    except (ValueError, OSError) as exc:
        catalog_ok, catalog_detail = False, str(exc)
        if not (skill_dir / 'skill.schema.json').exists():
            standard_summary = _standard_skill_summary(skill_dir)
            if standard_summary:
                catalog_detail = '标准 SKILL.md 身份字段可读；缺少平台运行 Schema（输入、输出、资源、版本）'
    scan_ok, scan_detail = check_static_scan(skill_dir)
    docs_ok, docs_detail = check_documentation(skill_dir, meta) if meta else (False, 'Catalog 无效，无法检查文档')
    if standard_summary:
        docs_detail = 'SKILL.md 有名称与描述；缺平台输入输出说明和 skill-card.md 核验'
    return {
        'skill': skill_dir.name, 'version': meta.get('version') if meta else None,
        'status': 'QUARANTINED', 'risk': 'HIGH', 'can_download': False, 'can_run': False,
        'standard_skill_summary': standard_summary,
        'checks': [
            {'name': 'Catalog', 'status': 'PASS' if catalog_ok else 'PARTIAL' if standard_summary else 'FAIL', 'detail': catalog_detail},
            {'name': 'Scanned', 'status': 'PASS' if scan_ok else 'FAIL', 'detail': scan_detail},
            {'name': 'Signed', 'status': 'UNVERIFIED', 'detail': '上传包没有可验证的受信任签名；仓库本地哈希清单不用于自动信任上传包'},
            {'name': 'Evaluated', 'status': 'NOT_RUN', 'detail': '隔离区禁止执行上传代码，未运行样例'},
            {'name': 'Documented', 'status': 'PASS' if docs_ok else 'PARTIAL' if standard_summary else 'FAIL', 'detail': docs_detail},
        ],
        'verdict': '隔离待审；不可下载、加入画布或运行。静态检查通过不代表安全。',
    }


def import_package(root: Path, owner_id: int, payload: bytes, provenance: dict | None = None) -> dict:
    if len(payload) > MAX_ZIP_BYTES:
        raise PackageError('ZIP 大小超过 5 MiB')
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        entries, skill_name = _members(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError('无效 ZIP 文件') from exc
    owner_dir = root / str(owner_id)
    owner_dir.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    destination = owner_dir / upload_id
    with tempfile.TemporaryDirectory(prefix='upload-', dir=owner_dir) as temp:
        temp_path = Path(temp)
        expanded = 0
        for item in entries:
            target = temp_path.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open('xb') as output:
                while block := source.read(1024 * 1024):
                    expanded += len(block)
                    if expanded > MAX_EXPANDED_BYTES:
                        raise PackageError('实际解压大小超过 20 MiB')
                    output.write(block)
        archive.close()
        skill_dir = temp_path / skill_name
        report = static_report(skill_dir)
        record = {'id': upload_id, 'owner_id': owner_id, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'content_sha256': _content_digest(skill_dir), 'entry_state': 'QUARANTINED',
                  'report': report}
        if provenance is not None:
            record['provenance'] = provenance
        (temp_path / 'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        shutil.move(str(temp_path), str(destination))
    return record


def read_upload(root: Path, owner_id: int, upload_id: str) -> dict | None:
    if not re.fullmatch(r'[0-9a-f]{32}', upload_id):
        return None
    path = root / str(owner_id) / upload_id / 'record.json'
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    expected = record.get('content_sha256')
    try:
        actual = _content_digest(path.parent / record['report']['skill']) if expected else None
    except (OSError, PackageError):
        actual = None
    record['content_integrity'] = ('PASS' if actual == expected else 'FAIL') if expected else 'UNVERIFIED'
    record['workflow_state'] = record.get('entry_state', 'QUARANTINED')
    record['can_download'] = False
    record['can_run'] = False
    return record


def list_uploads(root: Path, owner_id: int) -> list[dict]:
    owner_dir = root / str(owner_id)
    if not owner_dir.is_dir():
        return []
    return [record for child in sorted(owner_dir.iterdir()) if child.is_dir() if (record := read_upload(root, owner_id, child.name))]


