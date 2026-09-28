from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Dict, Any
import uuid
import json
import os
import socket
import io
import re
import hashlib
import zipfile
import time
from threading import Lock
from datetime import datetime
import urllib.error
import urllib.request

PROJECT_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get('SKILLPULSE_WORKSPACE_DIR', str(PROJECT_DIR / 'workspace'))).resolve()
SKILLS_DIR = BASE_DIR / 'skills'
OUTBOX_DIR = Path(os.environ.get('SKILLPULSE_OUTBOX_DIR', str(BASE_DIR / 'outbox'))).resolve()
REPORTS_DIR = OUTBOX_DIR / 'reports'
CATALOG_DIR = OUTBOX_DIR / 'catalogs'
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_DIR.mkdir(parents=True, exist_ok=True)

CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "skills.json"
CATALOG = []
if CATALOG_PATH.exists():
    try:
        CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        CATALOG = []
if not isinstance(CATALOG, list):
    CATALOG = []


app = FastAPI(title='SkillPulse Demo', version='0.3.0')

from fastapi.staticfiles import StaticFiles
from governance.schema import load_skill_schema, normalize_skill_meta
from engine.run_local import run_skill
from governance.checks import skill_health as evaluate_skill_health
from governance.audit import append_chained, verify_chain
from governance.versions import build_snapshot, store_snapshot, verify_snapshot, snapshot_path
from governance.discovery import (DiscoveryError, search_skill_repositories,
                          discover_skills_from_repos, build_quarantine_zip)
from stepfun_client import StepFunError, interpret as interpret_with_stepfun, status as stepfun_status
from gpu_semantic import SemanticUnavailable, rank as rank_on_cuda, status as gpu_semantic_status
from engine.controls import CONTROL_SCHEMAS, compatible_type, coerce_control_value, value_matches_type
from security.local_auth import CURRENT_ACTOR
from security.quarantine import (PackageError, MAX_ZIP_BYTES, import_package, list_uploads, read_upload)
from security.user_data import (save_workflow as persist_workflow, read_workflow, list_workflows,
                                read_run, list_runs)
app.mount('/frontend', StaticFiles(directory=str(Path(__file__).resolve().parent.parent / 'frontend'), html=True), name='frontend')

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)

PRIVATE_SKILLS_DIR = OUTBOX_DIR / 'private_skills'
USER_WORKFLOWS_DIR = OUTBOX_DIR / 'user_workflows'
USER_RUNS_DIR = OUTBOX_DIR / 'user_runs'
AI_CALLS: Dict[int, List[float]] = {}
AI_CALLS_LOCK = Lock()



@app.middleware('http')
async def require_local_session(request: Request, call_next):
    # 本地演示：无鉴权，固定以专家身份运行
    request.state.user = {'id': 1, 'username': 'demo', 'role': 'engineer'}
    actor_token = CURRENT_ACTOR.set(1)
    try:
        response = await call_next(request)
    finally:
        CURRENT_ACTOR.reset(actor_token)
    if request.url.path.startswith('/frontend'):
        response.headers['Cache-Control'] = 'no-store'
    return response









@app.post('/api/repository/upload')
async def repository_upload(request: Request, package: UploadFile = File(...)):
    if not package.filename or not package.filename.lower().endswith('.zip'):
        raise HTTPException(status_code=400, detail='只接受 ZIP 格式的 Skill 包')
    payload = await package.read(MAX_ZIP_BYTES + 1)
    try:
        return import_package(PRIVATE_SKILLS_DIR, request.state.user['id'], payload)
    except PackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class GithubImportRequest(BaseModel):
    repository: str
    commit_sha: str
    skill_path: str


@app.post('/api/repository/import-github')
def repository_import_github(request: Request, selection: GithubImportRequest):
    """只允许导入固定版本的单个 Skill 到隔离区；绝不自动准入。"""
    if selection.repository.count('/') != 1:
        raise HTTPException(status_code=422, detail='仓库标识无效')
    owner, repo = selection.repository.split('/', 1)
    try:
        payload = build_quarantine_zip(owner, repo, selection.commit_sha, selection.skill_path)
        provenance = {
            'platform': 'GitHub', 'repository': selection.repository,
            'source_url': f'https://github.com/{owner}/{repo}/tree/{selection.commit_sha}/{selection.skill_path}',
            'commit_sha': selection.commit_sha, 'skill_path': selection.skill_path,
            'origin_verified': False, 'signature_verified': False,
            'note': '已核对固定提交的 Git blob 哈希；未证明发布者身份或验证数字签名',
        }
        return import_package(PRIVATE_SKILLS_DIR, request.state.user['id'], payload, provenance=provenance)
    except (DiscoveryError, PackageError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get('/api/repository/uploads')
def repository_uploads(request: Request):
    return list_uploads(PRIVATE_SKILLS_DIR, request.state.user['id'])


@app.get('/api/repository/uploads/{upload_id}')
def repository_upload_detail(request: Request, upload_id: str):
    record = read_upload(PRIVATE_SKILLS_DIR, request.state.user['id'], upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail='未找到当前用户的上传记录')
    return record



class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: Optional[str] = 'step-3.7-flash'
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1024
    source: Optional[str] = 'auto'


class IntentRequest(BaseModel):
    task: str = Field(min_length=3, max_length=500)
    scene: str = Field(default='diagnostics', max_length=40)
    environment: str = Field(default='', max_length=160)

class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str]

class ChatUsage(BaseModel):
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]

class ChatResponse(BaseModel):
    id: str
    object: str = 'chat.completion'
    created: int
    model: str
    choices: List[ChatChoice]
    usage: ChatUsage

class SkillSummary(BaseModel):
    name: str
    path: str
    status: str
    catalog: str
    scanned: str
    evaluated: str
    signed: str
    documented: str
    risk: str = 'HIGH'
    origin_verified: bool = False
    signature_verified: bool = False
    integrity_verified: bool = False
    provenance_note: str = ''
    checks: List[dict] = []
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias='schema')

class HealthReport(BaseModel):
    skill: str
    status: str
    trust: str
    risk: str = 'HIGH'
    origin_verified: bool = False
    signature_verified: bool = False
    integrity_verified: bool = False
    provenance_note: str = ''
    version: Optional[str] = None
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias='schema')
    checks: List[dict]
    repair: List[str]
    verdict: str

class LegoNode(BaseModel):
    id: str
    name: str
    x: Optional[int] = None
    y: Optional[int] = None
    control_type: str = 'skill'
    input_type: str = 'any'
    output_type: str = 'any'
    node_schema: Optional[Dict[str, Any]] = None
    inputs: Any = {}
    model_config = ConfigDict(extra="ignore")

class LegoEdge(BaseModel):
    from_: str = Field(alias='from')
    to: str
    route: Optional[str] = None
    model_config = ConfigDict(populate_by_name=True)


class SkillSchema(BaseModel):
    title: str
    type: str = "object"
    required: List[str] = []
    properties: Dict[str, Any] = {}

class LegoRunRequest(BaseModel):
    nodes: List[LegoNode]
    edges: List[LegoEdge]
    inputs: Any = None
    selected_node: Optional[str] = None

class LegoRunResponse(BaseModel):
    passed: bool
    run_id: str
    started_at: str
    finished_at: str
    topology: Dict[str, Any]
    selected_node: Optional[Dict[str, Any]] = None
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    logs: List[Dict[str, Any]]
    artifacts: List[Dict[str, Any]]
    exports: Dict[str, str]

class AuditLog(BaseModel):
    time: str
    action: str
    target: str
    result: str
    detail: Optional[str] = None
    run_id: Optional[str] = None
    node_id: Optional[str] = None


class OperationAuditRequest(BaseModel):
    action: str
    target: str
    node_id: Optional[str] = None


class CompatibilityRequest(BaseModel):
    skill_name: str

class PipelineExportRequest(BaseModel):
    nodes: List[LegoNode]
    edges: List[LegoEdge]
    format: Optional[str] = 'yaml'
    generated_at: Optional[str] = None


@app.get('/api/controls')
def control_schemas():
    return CONTROL_SCHEMAS

AUDIT_PATH = REPORTS_DIR / "audit.jsonl"
AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
VERSIONS_DIR = REPORTS_DIR.parent / "versions"
VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

@app.get('/api/skills')
def list_skills() -> List[SkillSummary]:
    results = []
    if not SKILLS_DIR.exists():
        return results
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        report = _health_check(skill_dir)
        results.append(SkillSummary(
            name=skill_dir.name,
            path=str(skill_dir),
            status=report['status'],
            catalog=report['checks'][0]['status'],
            scanned=report['checks'][1]['status'],
            evaluated=report['checks'][3]['status'],
            signed=report['checks'][2]['status'],
            documented=report['checks'][4]['status'],
            risk=report['risk'],
            origin_verified=report['origin_verified'],
            signature_verified=report['signature_verified'],
            integrity_verified=report['integrity_verified'],
            provenance_note=report['provenance_note'],
            checks=report['checks'],
            schema=report.get('schema'),
        ))
    return results


def _rollback_skill_dir(skill_dir: Path) -> None:
    import shutil
    if skill_dir.exists() and skill_dir.is_dir():
        shutil.rmtree(skill_dir, ignore_errors=True)


def _extract_skill_zip(payload: bytes, dest: Path) -> str:
    """安全解压 GitHub 下载的 Skill zip 到可运行区；校验路径、符号链接、密钥文件与大小。"""
    import stat
    from pathlib import PurePosixPath
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(status_code=422, detail=f'下载的 Skill 包无效: {exc}') from exc
    files = [item for item in archive.infolist() if not item.is_dir()]
    if not files or len(files) > 100:
        raise HTTPException(status_code=422, detail='Skill 文件数超出限制（1-100）')
    if sum(item.file_size for item in files) > 20 * 1024 * 1024:
        raise HTTPException(status_code=422, detail='Skill 解压后超过 20 MiB')
    roots, seen = set(), set()
    for item in archive.infolist():
        name = item.filename
        parts = PurePosixPath(name).parts
        if not parts or name.startswith('/') or '\\' in name or ':' in name or '.' in parts or '..' in parts:
            raise HTTPException(status_code=422, detail='Skill 包含不安全路径')
        if len(parts) < 2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', parts[0]):
            raise HTTPException(status_code=422, detail='Skill 顶层目录无效')
        if name.casefold() in seen:
            raise HTTPException(status_code=422, detail='Skill 包含重复文件名')
        seen.add(name.casefold())
        roots.add(parts[0])
        if stat.S_ISLNK(item.external_attr >> 16):
            raise HTTPException(status_code=422, detail='Skill 不允许符号链接')
        if any(part.lower() in {'id_rsa', 'id_ed25519', 'id_ecdsa', '.env', 'private_key', 'secret'}
               for part in parts):
            raise HTTPException(status_code=422, detail='Skill 包含疑似密钥文件，已拒绝')
    if len(roots) != 1:
        raise HTTPException(status_code=422, detail='Skill 包只能包含一个顶层目录')
    skill_name = roots.pop()
    target = (dest / skill_name).resolve()
    if target.parent != dest.resolve():
        raise HTTPException(status_code=422, detail='Skill 路径越界')
    if target.exists():
        raise HTTPException(status_code=409, detail='可运行区已存在同名 Skill，未覆盖；可先在本地移除旧版本')
    dest.mkdir(parents=True, exist_ok=True)
    archive.extractall(target)
    return skill_name


def _skill_dir(skill_name: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', skill_name):
        raise HTTPException(status_code=400, detail='Invalid skill name')
    skill_dir = (SKILLS_DIR / skill_name).resolve()
    if skill_dir.parent != SKILLS_DIR.resolve() or not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail='Skill not found')
    return skill_dir

@app.get('/api/skills/{skill_name}/health', response_model=HealthReport)
def skill_health(skill_name: str) -> HealthReport:
    skill_dir = _skill_dir(skill_name)
    report = _health_check(skill_dir)
    return HealthReport(**report)


@app.get('/api/skills/{skill_name}/download')
def download_skill(skill_name: str):
    """仅打包当前全部检查通过的本地 Skill；ZIP 在内存生成，不写入项目目录。"""
    skill_dir = _skill_dir(skill_name)
    report = _health_check(skill_dir)
    if report['status'] != 'HEALTHY':
        raise HTTPException(status_code=403, detail={
            'message': 'Skill 未通过可信准入，禁止下载',
            'risk': report['risk'],
            'origin_verified': report['origin_verified'],
            'signature_verified': report['signature_verified'],
            'integrity_verified': report['integrity_verified'],
            'provenance_note': report['provenance_note'],
            'checks': report['checks'],
        })
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(skill_dir.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc' and path.name != 'skill.oms.sig':
                archive.write(path, Path(skill_name) / path.relative_to(skill_dir))
    _append_audit({'time': _now(), 'action': 'operation-download', 'target': skill_name,
                   'result': 'recorded', 'detail': None, 'run_id': None})
    return Response(
        content=buffer.getvalue(),
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{skill_name}.zip"'},
    )

class GithubInstallRequest(BaseModel):
    repository: str
    commit_sha: str
    skill_path: str


@app.post('/api/skills/install-from-github')
def install_skill_from_github(request: Request, selection: GithubInstallRequest):
    """下载 GitHub 固定提交的单个 Skill -> 解压到可运行区 -> 跑完整五道安检；HEALTHY 才准入，否则回滚。"""
    if selection.repository.count('/') != 1:
        raise HTTPException(status_code=422, detail='仓库标识无效')
    owner, repo = selection.repository.split('/', 1)
    try:
        payload = build_quarantine_zip(owner, repo, selection.commit_sha, selection.skill_path)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    skill_name = _extract_skill_zip(payload, SKILLS_DIR)
    skill_dir = (SKILLS_DIR / skill_name).resolve()
    try:
        report = _health_check(skill_dir, integrity_verified=True)
    except Exception as exc:
        _rollback_skill_dir(skill_dir)
        raise HTTPException(status_code=500, detail='下载成功但安检失败，已回滚: ' + str(exc)) from exc
    if report['status'] != 'HEALTHY':
        # 红级：不放行可运行区，自动转入隔离区供人工审查，同时返回报告让前端可展示原因。
        q_prov = {
            'platform': 'GitHub',
            'repository': selection.repository,
            'source_url': f'https://github.com/{owner}/{repo}/tree/{selection.commit_sha}/{selection.skill_path}',
            'commit_sha': selection.commit_sha,
            'skill_path': selection.skill_path,
            'origin_verified': False,
            'signature_verified': False,
            'note': f'下载并安检未通过（{report["status"]}），自动转入隔离区，等待人工审查',
        }
        quarantined = False
        try:
            import_package(PRIVATE_SKILLS_DIR, request.state.user['id'], payload, provenance=q_prov)
            quarantined = True
        except Exception:
            quarantined = False
        _rollback_skill_dir(skill_dir)
        _append_audit({'time': _now(), 'action': 'install-from-github', 'target': skill_name,
                       'result': 'quarantined-not-healthy', 'quarantined': quarantined,
                       'detail': f'{owner}/{repo}@{selection.commit_sha[:12]} {selection.skill_path}',
                       'run_id': None})
        report['installed'] = False
        report['quarantined'] = quarantined
        report['source'] = {'platform': 'GitHub', 'repository': selection.repository,
                            'commit_sha': selection.commit_sha, 'skill_path': selection.skill_path}
        return report
    _append_audit({'time': _now(), 'action': 'install-from-github', 'target': skill_name,
                   'result': 'installed', 'detail': f'{owner}/{repo}@{selection.commit_sha[:12]} {selection.skill_path}',
                   'run_id': None})
    report['installed'] = True
    report['source'] = {'platform': 'GitHub', 'repository': selection.repository,
                        'commit_sha': selection.commit_sha, 'skill_path': selection.skill_path}
    return report


@app.post('/api/skills/{skill_name}/repair')
def repair_skill(skill_name: str):
    _skill_dir(skill_name)
    raise HTTPException(status_code=409, detail='本轮禁用自动修复：自动补文档或伪造签名会产生虚假的可信状态。请人工修复后重新安检。')

@app.get('/api/reports')
def list_reports() -> List[dict]:
    reports = []
    if REPORTS_DIR.exists():
        for path in sorted(REPORTS_DIR.glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                reports.append({'file': path.name, 'skill': data.get('skill'), 'status': data.get('status'), 'time': data.get('time')})
            except Exception:
                pass
    return reports

@app.get('/api/reports/{skill_name}')
def get_report(skill_name: str):
    path = REPORTS_DIR / f'{skill_name}.json'
    if not path.exists():
        raise HTTPException(status_code=404, detail='Report not found')
    return json.loads(path.read_text(encoding='utf-8'))

@app.get('/api/demo/download-report/{skill_name}')
def download_report(skill_name: str):
    skill_dir = _skill_dir(skill_name)
    report = _health_check(skill_dir)
    report['time'] = _now()
    return Response(
        content=json.dumps(report, ensure_ascii=False, indent=2),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="{skill_name}-report.json"'},
    )

@app.get('/api/lego/skills')
def lego_skills() -> List[Dict[str, Any]]:
    results = []
    if not SKILLS_DIR.exists():
        return results
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        report = _health_check(skill_dir)
        results.append({
            'name': skill_dir.name,
            'source': next((item.get('source') for item in CATALOG if item.get('name') == skill_dir.name), 'local_project'),
            'status': report['status'],
            'trust': report['trust'],
            'risk': report['risk'],
            'origin_verified': report['origin_verified'],
            'signature_verified': report['signature_verified'],
            'integrity_verified': report['integrity_verified'],
            'provenance_note': report['provenance_note'],
            'use': _infer_use(skill_dir.name),
            'description': (report.get('schema') or {}).get('description', ''),
            'schema': report.get('schema'),
            'version': report.get('version'),
            'checks': report['checks'],
        })
    return results

@app.post('/api/pipelines/run', response_model=LegoRunResponse)
def lego_run(req: LegoRunRequest):
    _require_audit_integrity()
    started_at = _now()
    if not req.nodes:
        raise HTTPException(status_code=400, detail='nodes required')
    node_names = [n.name for n in req.nodes if n.control_type == 'skill']
    seen = set(node_names)
    if len(seen) != len(node_names):
        raise HTTPException(status_code=400, detail='duplicate skill nodes in pipeline')
    node_map = {n.id: {
        'id': n.id, 'name': n.name, 'x': n.x, 'y': n.y,
        'control_type': n.control_type, 'input_type': n.input_type, 'output_type': n.output_type,
        'node_schema': n.node_schema or {}, 'inputs': n.inputs or {}
    } for n in req.nodes}
    if len(node_map) != len(req.nodes):
        raise HTTPException(status_code=400, detail='duplicate node ids in pipeline')
    for edge in req.edges:
        if edge.from_ not in node_map or edge.to not in node_map:
            raise HTTPException(status_code=422, detail='连线引用了不存在的节点')
        if edge.from_ == edge.to:
            raise HTTPException(status_code=422, detail='节点不能连接自身')
        source_type = node_map[edge.from_]['control_type']
        if source_type == 'if_branch':
            if edge.route not in ('true', 'false'):
                raise HTTPException(status_code=422, detail='分支出线必须指定 route=true 或 route=false')
        elif edge.route is not None:
            raise HTTPException(status_code=422, detail='只有分支出线可以指定 route')
    if len({(e.from_, e.to, e.route) for e in req.edges}) != len(req.edges):
        raise HTTPException(status_code=422, detail='重复连线')
    for node_id, node in node_map.items():
        if node['control_type'] == 'if_branch':
            routes = [e.route for e in req.edges if e.from_ == node_id]
            if not (req.selected_node == node_id and len(req.nodes) == 1 and not req.edges) and sorted(routes) != ['false', 'true']:
                raise HTTPException(status_code=422, detail='分支节点必须分别连接一条 true 和 false 出线')
    node_health = {}
    for node_id, node in node_map.items():
        if node['control_type'] in CONTROL_SCHEMAS:
            if node['name'] != node['control_type']:
                raise HTTPException(status_code=422, detail='控制节点 name 与 control_type 不一致')
            node_health[node_id] = {
                'status': 'HEALTHY', 'trust': 'LOCAL', 'risk': 'LOW',
                'version': CONTROL_SCHEMAS[node['control_type']]['version'],
                'schema': CONTROL_SCHEMAS[node['control_type']], 'checks': [],
            }
            node['node_schema'] = node_health[node_id]['schema']
            continue
        if node['control_type'] != 'skill':
            raise HTTPException(status_code=422, detail=f'不支持的控制节点: {node["name"]}')
        health = _health_check(_skill_dir(node['name']))
        if health['status'] != 'HEALTHY':
            raise HTTPException(status_code=403, detail={
                'message': f'Skill 未通过可信准入，禁止运行: {node["name"]}',
                'risk': health['risk'], 'checks': health['checks'],
            })
        node_health[node_id] = health
        # Schema is always sourced from the validated on-disk Skill contract, never the request body.
        node['node_schema'] = health['schema']
    for e in req.edges:
        src = node_map.get(e.from_)
        dst = node_map.get(e.to)
        if not src or not dst:
            continue
        out_types = {port['type'] for port in node_health[e.from_]['schema']['outputs']}
        destination_ports = node_health[e.to]['schema']['inputs']
        if dst['control_type'] in CONTROL_SCHEMAS:
            destination_ports = [port for port in destination_ports if port['name'] == 'value']
        in_types = {port['type'] for port in destination_ports}
        compatible = any(compatible_type(a, b) for a in out_types for b in in_types)
        if not compatible:
            raise HTTPException(status_code=422, detail='类型不匹配：' + src['name'] + ' 输出 ' + ','.join(sorted(out_types)) + ' -> ' + dst['name'] + ' 输入 ' + ','.join(sorted(in_types)))
    control_inputs = {nid: dict(node.get('inputs') or {}) for nid, node in node_map.items()}
    if isinstance(req.inputs, dict):
        roots = [nid for nid in node_map if not any(e.to == nid for e in req.edges)]
        for nid in node_map:
            if isinstance(req.inputs.get(nid), dict):
                control_inputs[nid].update(req.inputs[nid])
            elif nid in roots and not any(isinstance(value, dict) for value in req.inputs.values()):
                control_inputs[nid].update({k: v for k, v in req.inputs.items() if not str(k).startswith('_')})
    elif isinstance(req.inputs, list) and req.inputs:
        roots = [nid for nid in node_map if not any(e.to == nid for e in req.edges)]
        if roots:
            control_inputs[roots[0]].update({port['name']: req.inputs[i] for i, port in enumerate(node_health[roots[0]]['schema']['inputs']) if i < len(req.inputs)})
    for node_id, node in node_map.items():
        if node['control_type'] == 'loop':
            target_name = control_inputs[node_id].get('target_skill')
            if not isinstance(target_name, str) or not target_name.strip():
                raise HTTPException(status_code=422, detail='循环节点缺少 target_skill')
            target_health = _health_check(_skill_dir(target_name))
            if target_health['status'] != 'HEALTHY':
                raise HTTPException(status_code=403, detail='循环目标 Skill 未通过安检')
    involved_skills = [node['name'] for node in node_map.values() if node['control_type'] == 'skill']
    involved_skills += [control_inputs[node_id]['target_skill'] for node_id, node in node_map.items() if node['control_type'] == 'loop']
    if involved_skills:
        dependency_check = _dependency_compatibility(involved_skills)
        if not dependency_check['compatible']:
            raise HTTPException(status_code=422, detail={'message': '依赖声明存在冲突或无法验证，禁止运行', 'notes': dependency_check['notes']})
    ordered = []
    if req.edges:
        from collections import deque
        indegree = {n.id: 0 for n in req.nodes}
        adj = {n.id: [] for n in req.nodes}
        for e in req.edges:
            if e.from_ not in adj or e.to not in adj:
                continue
            adj[e.from_].append(e.to)
            indegree[e.to] += 1
        q = deque([nid for nid in node_map if indegree[nid] == 0])
        while q:
            cur = q.popleft()
            ordered.append(cur)
            for nxt in adj[cur]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    q.append(nxt)
        if len(ordered) != len(node_map):
            raise HTTPException(status_code=422, detail='拓扑包含循环依赖')
    else:
        ordered = [n.id for n in req.nodes]
    new_nodes = []
    logs = []
    passed = True
    run_id = uuid.uuid4().hex[:8]
    edge_dicts = []
    active_edges = set()
    outputs_by_node = {}
    for nid in ordered:
        skill = node_map[nid]
        result = None
        incoming = [(index, edge) for index, edge in enumerate(req.edges) if edge.to == nid]
        skipped = bool(incoming) and not any(index in active_edges for index, _ in incoming)
        if skipped:
            provided = {}
            result = {'ok': False, 'outputs': {}, 'artifacts': [], 'skipped': True, 'elapsed_ms': 0}
        else:
            provided = dict(control_inputs.get(nid, {}))
            meta = node_health[nid]['schema']
            for edge_index, edge in incoming:
                if edge_index not in active_edges:
                    continue
                parent_outputs = outputs_by_node.get(edge.from_, {})
                parent_ports = node_health[edge.from_]['schema']['outputs']
                input_ports = meta['inputs']
                if skill['control_type'] in CONTROL_SCHEMAS and parent_outputs:
                    provided['value'] = parent_outputs.get('value', next(iter(parent_outputs.values())))
                    continue
                if node_map[edge.from_]['control_type'] in CONTROL_SCHEMAS and len(input_ports) == 1:
                    provided[input_ports[0]['name']] = parent_outputs.get('value')
                    continue
                for port in input_ports:
                    if port['name'] in parent_outputs:
                        provided[port['name']] = parent_outputs[port['name']]
                    elif len(parent_ports) == 1 and len(input_ports) == 1 and parent_ports[0]['name'] in parent_outputs:
                        provided[port['name']] = parent_outputs[parent_ports[0]['name']]
            for port in meta['inputs']:
                key = port['name']
                if port['type'] != 'json' or not isinstance(provided.get(key), str):
                    continue
                if not provided[key].strip() and not port['required']:
                    provided.pop(key)
                else:
                    try:
                        provided[key] = json.loads(provided[key])
                    except json.JSONDecodeError:
                        pass  # 下方输入类型校验返回可读的 422。
            missing = [
                port['name'] for port in meta['inputs']
                if port['required'] and (
                    port['name'] not in provided
                    or provided[port['name']] is None
                    or isinstance(provided[port['name']], str) and not provided[port['name']].strip()
                )
            ]
            if missing:
                raise HTTPException(status_code=422, detail=f'{skill["name"]} 缺少必填输入: {", ".join(missing)}')
            invalid = []
            for port in meta['inputs']:
                if port['name'] not in provided:
                    continue
                value, kind = provided[port['name']], port['type']
                if skill['control_type'] in CONTROL_SCHEMAS:
                    value = coerce_control_value(value, kind)
                    provided[port['name']] = value
                if not value_matches_type(value, kind):
                    invalid.append(port['name'] + ':' + kind)
            if invalid:
                raise HTTPException(status_code=422, detail=f'{skill["name"]} 输入类型不匹配: {", ".join(invalid)}')
            control_type = skill.get('control_type', 'skill')
            if control_type == 'if_branch':
                route = 'true' if provided['condition'] else 'false'
                result = {
                    'ok': True,
                    'outputs': {'value': provided['value']}, 'route': route,
                    'artifacts': [],
                    'elapsed_ms': 0,
                }
            elif control_type == 'loop':
                count = provided['count']
                if count < 1 or count > 5:
                    raise HTTPException(status_code=422, detail='循环次数必须在 1–5 之间')
                target_name = provided['target_skill']
                target_report = _health_check(_skill_dir(target_name))
                if target_report['status'] != 'HEALTHY':
                    raise HTTPException(status_code=403, detail='循环目标 Skill 未通过安检')
                target_inputs = target_report['schema']['inputs']
                target_outputs = target_report['schema']['outputs']
                required_inputs = [port for port in target_inputs if port['required']]
                if len(required_inputs) != 1 or len(target_outputs) != 1:
                    raise HTTPException(status_code=422, detail='循环目标 Skill 必须恰有一个必填输入和一个输出端口')
                current_value = provided['value']
                iterations = []
                for _ in range(count):
                    if not value_matches_type(current_value, required_inputs[0]['type']):
                        raise HTTPException(status_code=422, detail='循环输入与目标 Skill 端口类型不匹配')
                    try:
                        item = run_skill(target_name, {required_inputs[0]['name']: current_value})
                    except Exception:
                        item = {'ok': False, 'outputs': {}, 'artifacts': [], 'error': 'runner_failed'}
                    iterations.append(item)
                    if not item.get('ok'):
                        break
                    current_value = item.get('outputs', {}).get(target_outputs[0]['name'])
                    if not value_matches_type(current_value, target_outputs[0]['type']):
                        raise HTTPException(status_code=422, detail='循环目标 Skill 输出类型与声明不符')
                ok = all(item.get('ok') for item in iterations)
                result = {
                    'ok': ok,
                    'outputs': {'value': current_value, 'results': [item.get('outputs') for item in iterations]},
                    'artifacts': [],
                    'error': 'loop_target_failed' if not ok else None,
                    'elapsed_ms': sum(item.get('elapsed_ms', 0) for item in iterations),
                    'iterations': len(iterations), 'target_skill': target_name,
                }
            else:
                try:
                    result = run_skill(skill['name'], provided)
                except Exception as exc:
                    result = {'ok': False, 'outputs': {}, 'artifacts': [], 'error': str(exc)}
            if result.get('ok'):
                outputs_by_node[nid] = result.get('outputs') or {}
                for edge_index, edge in enumerate(req.edges):
                    if edge.from_ == nid and (control_type != 'if_branch' or edge.route == result['route']):
                        active_edges.add(edge_index)
            else:
                passed = False
        report = node_health[nid]
        healthy = report['status'] == 'HEALTHY'
        if not healthy:
            passed = False
        status = report['status']
        trust = report['trust']
        failed = [] if healthy else [c['name'] for c in report['checks'] if c['status'] != 'PASS']
        status = 'SKIPPED' if skipped else 'HEALTHY' if bool(result and result.get('ok')) else 'UNHEALTHY'
        trust = report['trust'] if status == 'HEALTHY' else 'LOW'
        failed = [] if status in ('HEALTHY', 'SKIPPED') else [str((result or {}).get('error') or 'runner_failed')]
        input_bytes = json.dumps(provided, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
        output_bytes = json.dumps((result or {}).get('outputs') or {}, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
        result['input_summary'] = {'fields': sorted(provided), 'sha256': hashlib.sha256(input_bytes).hexdigest()}
        result['output_summary'] = {'fields': sorted(((result or {}).get('outputs') or {}).keys()), 'sha256': hashlib.sha256(output_bytes).hexdigest()}
        node_summary = {
            'id': skill['id'],
            'name': skill['name'],
            'version': report.get('version'),
            'x': skill['x'],
            'y': skill['y'],
            'status': status,
            'trust': trust,
            'control_type': skill.get('control_type', 'skill'),
            'input_type': skill.get('input_type', 'any'),
            'output_type': skill.get('output_type', 'any'),
            'schema': skill.get('node_schema') or {},
            'failed': failed,
            'checks': report['checks'],
            'result': result,
            'outputs': result.get('outputs') or {},
            'elapsed_ms': result.get('elapsed_ms'),
            'input_summary': result['input_summary'],
            'output_summary': result['output_summary'],
        }
        if skill['name'] == 'nvidia-skill-card-validator':
            node_summary['source'] = (result or {}).get('source')
        new_nodes.append(node_summary)
        logs.append({'time': _now(), 'action': 'skip' if skipped else 'run', 'target': skill['name'], 'result': '跳过' if skipped else '通过' if healthy and status == 'HEALTHY' else '阻断', 'detail': f'v{report.get("version")} · {(result or {}).get("elapsed_ms", 0)}ms'})
        _append_audit({
            'time': started_at,
            'action': 'skip-node' if skipped else 'run-node',
            'target': skill['name'],
            'result': node_summary['status'],
            'detail': json.dumps({'version': report.get('version'), 'input_summary': result['input_summary'], 'output_summary': result['output_summary'], 'source': node_summary.get('source'), 'error': 'runner_failed' if not result.get('ok') else None}, ensure_ascii=False),
            'run_id': run_id,
            'node_id': nid,
        })
    edge_dicts = [{'from': e.from_, 'to': e.to, 'route': e.route, 'active': index in active_edges} for index, e in enumerate(req.edges)]
    for index, e in enumerate(req.edges):
        edge_active = index in active_edges
        logs.append({'time': _now(), 'action': 'link', 'target': node_map.get(e.from_, {}).get('name') + ' -> ' + node_map.get(e.to, {}).get('name'), 'result': '已连接' if edge_active else '未走此路径', 'detail': '链路'})
        _append_audit({
            'time': _now(),
            'action': 'link',
            'target': node_map.get(e.from_, {}).get('name') + ' -> ' + node_map.get(e.to, {}).get('name'),
            'result': '已连接' if edge_active else '未走此路径',
            'detail': '链路',
            'run_id': run_id,
        })
    logs.append({'time': _now(), 'action': 'verdict', 'target': 'pipeline', 'result': '通过' if passed else '阻断', 'detail': 'SkillPulse 治理检查 + 运行结果'})
    run_finished = datetime.now()
    artifacts = []
    exports = {'json': '/api/pipelines/export?run_id=' + run_id, 'openapi': '/api/pipelines/export?format=openapi&run_id=' + run_id}
    selected_node = None
    if req.selected_node:
        selected = next((n for n in new_nodes if n['id'] == req.selected_node), None)
        if selected:
            selected_node = {
                'id': selected['id'],
                'name': selected['name'],
                'status': selected['status'],
                'trust': selected['trust'],
                'control_type': selected.get('control_type', 'skill'),
                'outputs': (selected.get('result') or {}).get('outputs') or {},
                'artifacts': (selected.get('result') or {}).get('artifacts') or [],
                'error': (selected.get('result') or {}).get('error'),
                'elapsed_ms': (selected.get('result') or {}).get('elapsed_ms'),
            }
            if selected_node['artifacts']:
                artifacts.extend(selected_node['artifacts'] if isinstance(selected_node['artifacts'], list) else [selected_node['artifacts']])
    if not selected_node:
        first = next((n for n in new_nodes if n['status'] == 'HEALTHY'), new_nodes[0] if new_nodes else None)
        if first:
            selected_node = {
                'id': first['id'],
                'name': first['name'],
                'status': first['status'],
                'trust': first['trust'],
                'control_type': first.get('control_type', 'skill'),
                'outputs': (first.get('result') or {}).get('outputs') or {},
                'artifacts': (first.get('result') or {}).get('artifacts') or [],
                'error': (first.get('result') or {}).get('error'),
                'elapsed_ms': (first.get('result') or {}).get('elapsed_ms'),
            }
    actor_id = CURRENT_ACTOR.get()
    run_folder = USER_RUNS_DIR / str(actor_id) if actor_id is not None else CATALOG_DIR
    run_folder.mkdir(parents=True, exist_ok=True)
    pipeline_file = run_folder / f'pipeline-{run_id}.json'
    safe_request = {
        'nodes': [{'id': node_id, 'name': node['name'], 'version': node_health[node_id].get('version')} for node_id, node in node_map.items()],
        'edges': [{'from': edge.from_, 'to': edge.to, 'route': edge.route} for edge in req.edges],
        'input_fields': sorted(req.inputs.keys()) if isinstance(req.inputs, dict) else [],
        'input_sha256': hashlib.sha256(json.dumps(req.inputs, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest() if req.inputs is not None else None,
    }
    safe_nodes = [{
        'id': node['id'], 'name': node['name'], 'version': node.get('version'),
        'status': node['status'], 'risk': node_health[node['id']]['risk'],
        'input_summary': node['input_summary'], 'output_summary': node['output_summary'],
        'elapsed_ms': node.get('elapsed_ms'),
        'error': 'runner_failed' if node['status'] == 'UNHEALTHY' else None,
        'checks': node['checks'],
        'source': node.get('source'),
    } for node in new_nodes]
    payload = {
        'run_id': run_id,
        'started_at': started_at,
        'finished_at': run_finished.strftime('%Y-%m-%d %H:%M:%S'),
        'request_summary': safe_request,
        'response_summary': {'passed': passed, 'nodes': safe_nodes, 'edges': edge_dicts, 'logs': logs},
    }
    if actor_id is not None:
        payload['owner_id'] = actor_id
    record_sha256 = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    pipeline_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    _append_audit({
        'time': run_finished.strftime('%Y-%m-%d %H:%M:%S'),
        'action': 'run',
        'target': 'pipeline',
        'result': '通过' if passed else '阻断',
        'detail': json.dumps({'run_id': run_id, 'nodes': len(new_nodes), 'record_sha256': record_sha256}, ensure_ascii=False),
        'run_id': run_id,
    })
    import sys
    return LegoRunResponse(
        run_id=run_id,
        started_at=started_at,
        finished_at=run_finished.strftime('%Y-%m-%d %H:%M:%S'),
        passed=passed,
        topology={'nodes': len(new_nodes), 'edges': len(req.edges)},
        selected_node=selected_node,
        nodes=[{k: v for k, v in n.items() if k != 'result'} for n in new_nodes],
        edges=edge_dicts,
        logs=logs,
        artifacts=artifacts,
        exports=exports,
    )

def search_local_catalog(query: str, source: str = 'auto', limit: int = 8) -> List[dict]:
    """可复现的本地词项匹配；只返回当前磁盘上真实存在的 Skill。"""
    items = [item for item in CATALOG if isinstance(item, dict) and
             (SKILLS_DIR / str(item.get('name', ''))).is_dir() and
             (source in {'auto', 'local'} or item.get('source') == source)]
    if source not in {'auto', 'local', 'local_project', 'local_sample'}:
        return []
    q = (query or '').strip().lower()[:200]
    aliases = {'知识库': 'rag', '问答': 'qa', '健康': 'health', '诊断': 'doctor',
               '安检': 'skill', '安全': 'trust', '合规': 'report', '报告': 'report',
               '图片': 'image', '图像': 'image', '视觉': 'vision', '检测': 'detection',
               '视频': 'video', '优化': 'optimization', '数据': 'data', '文件': 'file',
               '文档': 'document', '代码': 'code', '测试': 'test', '部署': 'deploy',
               '翻译': 'translate', '摘要': 'summary', '总结': 'summary', '提取': 'extract',
               '分类': 'classify', '生成': 'generate', '分析': 'analysis', '写作': 'write',
               '音频': 'audio', '语音': 'audio', '机器人': 'robot', '搜索': 'search',
               '检索': 'rag', '聊天': 'chat', '对话': 'chat', '对象': 'detection',
               '定位': 'grounding', '标注': 'annotation', '模型': 'model', '训练': 'train',
               '推理': 'inference', '嵌入': 'embedding', '审计': 'audit', '检查': 'health',
               '评估': 'evaluate', '验证': 'validate', '修复': 'fix', '建议': 'advice',
               '目标': 'detection', '摘要视频': 'video', '视频搜索': 'video'}
    terms = list(dict.fromkeys(re.findall(r'[a-z][a-z0-9_.-]{1,31}', q) +
                               [term for phrase, term in aliases.items() if phrase in q]))
    # 中文能力词：直接取 query 中 2-6 字中文片段做子串匹配（排除助词与演示拼接词）。
    cn_noise = {'帮我', '一个', '一下', '这个', '那个', '进行', '可以', '需要', '想要', '希望',
                '给我', '通过', '使用', '利用', '基于', '我们', '还有', '请帮', '能够', '看看',
                '场景', '运行环境', '本机演示', '未验证', '公开', '候选', '本地', '在线', '搜索',
                '匹配', '然后', '接着', '最后', '给我一', '一份', '帮我做', '帮我写'}
    zh_phrases = [p for p in re.findall(r'[\u4e00-\u9fff]{2,6}', q) if p not in cn_noise]
    # “Skill / Agent / 场景”属于宽泛词；有明确能力词时不让它们单独召回无关资产。
    broad_terms = {'skill', 'agent', 'local', 'general', 'diagnostics'}
    specific_terms = set(terms) - broad_terms
    if q and not terms and not zh_phrases:
        return []
    scored = []
    for item in items:
        name_tags = ' '.join([str(item.get('name', '')), ' '.join(item.get('tags', [])),
                              str(item.get('scene', ''))]).lower()
        description = ' '.join([str(item.get('use', '')), str(item.get('summary', ''))]).lower()
        haystack = name_tags + ' ' + description
        matched = [term for term in terms if term in name_tags or term in description]
        matched += [phrase for phrase in zh_phrases if phrase in haystack]
        if q and not matched:
            continue
        if specific_terms and not (set(matched) & specific_terms):
            continue
        score = sum(3 if term in name_tags else 1 for term in matched) if q else 1
        scored.append((score, {**item, 'score': score, 'matched_terms': matched}))
    scored.sort(key=lambda x: (-x[0], x[1].get('name','')))
    return [entry for _, entry in scored[:max(1, limit)]]

def fetch_github_nvidia_skills(query: str, limit: int = 6) -> List[dict]:
    q = urllib.parse.quote(query or 'nvidia/skills')
    url = f'https://api.github.com/search/repositories?q={q}&sort=updated&per_page={limit}'
    req = urllib.request.Request(url, headers={'Accept':'application/vnd.github+json','User-Agent':'SkillPulse-Demo'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return []
    items = []
    for repo in body.get('items', [])[:limit]:
        items.append({
            'name': repo.get('full_name') or repo.get('name') or 'github-repo',
            'scene': 'general',
            'source': 'github',
            'trust': 'MEDIUM',
            'status': 'UNHEALTHY',
            'use': repo.get('description') or 'GitHub repository result',
            'summary': repo.get('description') or '',
            'tags': ['github','repo'],
            'color': '#fee2e2',
        })
    return items

@app.post('/api/search/skills')
def search_skills(req: ChatRequest):
    query = (req.messages[-1].content if req.messages else '') if req.messages else ''
    source = getattr(req, 'source', 'auto') if hasattr(req, 'source') else 'auto'
    results = search_local_catalog(query, source=source, limit=8)
    normalized = []
    for item in results:
        skill_dir = _skill_dir(item['name'])
        report = _health_check(skill_dir)
        tokens = [token for token in query.lower().replace('/', ' ').replace('-', ' ').split() if token]
        haystack = ' '.join([item.get('name', ''), item.get('use', ''), item.get('summary', ''), ' '.join(item.get('tags', []))]).lower()
        matched = [token for token in tokens if token in haystack]
        normalized.append({
            **item,
            'status': report['status'],
            'trust': report['trust'],
            'risk': report['risk'],
            'checks': report['checks'],
            'schema': report.get('schema'),
            'version': report.get('version'),
            'match_hint': '本地词项匹配：' + (' / '.join(item.get('matched_terms', [])) if item.get('matched_terms') else '无查询词'),
            'origin_verified': False,
            'origin_note': '本地演示实现；不因名称或目录标签而获得上游发布者身份',
        })
    return {'query': query, 'results': normalized, 'mode': 'local-prototype-match'}


@app.get('/api/ai/status')
def ai_status():
    """只返回可用状态，不暴露密钥或账户资料。"""
    return stepfun_status()


@app.get('/api/search/semantic/status')
def semantic_status():
    return gpu_semantic_status()


@app.post('/api/search/semantic')
def semantic_search(req: IntentRequest):
    """仅在本地 CUDA 模型真实可用时检索；不把 CPU/词项回退伪装为 GPU 推理。"""
    candidates = [item for item in CATALOG if item.get('source') in {'local_project', 'local_sample'}
                  and isinstance(item.get('name'), str) and _skill_dir(item['name']).is_dir()]
    try:
        measured = rank_on_cuda(req.task, candidates)
    except SemanticUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    by_name = {item['name']: item for item in candidates}
    results = []
    for item in measured['results'][:8]:
        catalog_item = by_name[item['name']]
        report = _health_check(_skill_dir(item['name']))
        results.append({**catalog_item, 'semantic_score': item['score'],
                        'match_hint': f"CUDA 向量相似度 {item['score']:.4f}",
                        'status': report['status'], 'risk': report['risk'],
                        'trust': report['trust'], 'schema': report.get('schema')})
    return {**measured, 'results': results, 'trust_note': '排序不等于安检；下载和运行仍按本地健康状态判断'}


@app.post('/api/ai/interpret')
def ai_interpret(req: IntentRequest, request: Request):
    """用户主动触发云端解析；模型推荐仍须重新经过本地安检。"""
    if not stepfun_status()['available']:
        raise HTTPException(status_code=503, detail='未配置 StepFun API Key；本地检索仍可使用')
    actor_id = request.state.user['id']
    with AI_CALLS_LOCK:
        now = time.monotonic()
        recent = [instant for instant in AI_CALLS.get(actor_id, []) if now - instant < 3600]
        if len(recent) >= 8:
            raise HTTPException(status_code=429, detail='本账号一小时内的模型调用已达演示上限')
        recent.append(now)
        AI_CALLS[actor_id] = recent
    candidates = [item for item in CATALOG if item.get('source') in {'local_project', 'local_sample'}
                  and isinstance(item.get('name'), str) and _skill_dir(item['name']).is_dir()]
    try:
        parsed = interpret_with_stepfun(req.task, req.scene, candidates, req.environment)
    except StepFunError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    recommendations = []
    for name in parsed.get('recommended_skill_names', []):
        item = next((entry for entry in candidates if entry['name'] == name), None)
        if item is None:
            continue
        report = _health_check(_skill_dir(name))
        recommendations.append({'name': name, 'use': item.get('use', ''),
                                'catalog_summary': item.get('summary', ''),
                                'catalog_scene': item.get('scene', ''),
                                'model_match_reason': parsed.get('recommendation_reasons', {}).get(name, ''),
                                'status': report['status'], 'risk': report['risk'],
                                'eligible': report['status'] == 'HEALTHY',
                                'source': item['source']})
    return {**{key: value for key, value in parsed.items()
               if key not in {'recommended_skill_names', 'recommendation_reasons'}},
            'recommendations': recommendations,
            'trust_note': '模型只解释任务；下载与运行权限以实时本地安检为准'}


@app.post('/api/search/llm')
def llm_github_search(req: ChatRequest):
    """LLM×GitHub 搜索：LLM 生成搜索词 → GitHub search API 真实搜索 → 扫描 SKILL.md 候选。"""
    api_key = os.environ.get('STEPFUN_API_KEY', '')
    if not api_key:
        raise HTTPException(status_code=503, detail='未配置 STEPFUN_API_KEY；LLM×GitHub 搜索停用。请创建根目录 stepfun.env 配置 key 后重启。')
    task = (req.messages[-1].content if req.messages else '') if req.messages else ''
    region = (os.environ.get('STEPFUN_REGION') or 'china').strip().lower()
    llm_base = 'https://api.stepfun.ai/v1/chat/completions' if region == 'global' else 'https://api.stepfun.com/v1/chat/completions'
    body = {
        'model': req.model or 'step-3.7-flash',
        'messages': [
            {'role': 'system', 'content': '你是 GitHub Skill 搜索助手。根据用户任务，直接输出 1-3 个适合在 GitHub 搜索 agent skill 仓库的英文搜索词（短关键词组合）。第一行就必须是 JSON 字符串数组，例如 ["ppt generation"]。禁止任何分析、解释、思考过程、Markdown 或其他文字；禁止以 Got it、Let、First、Here 等词开头；没有思路就输出 []。'},
            {'role': 'user', 'content': task}
        ],
        'stream': False,
        'temperature': 0.2,
        'thinking': {'type': 'disabled'},
        'max_tokens': 1200,
    }
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req_obj = urllib.request.Request(
        llm_base,
        data=data,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req_obj, timeout=15) as resp:
            outer = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        raise HTTPException(status_code=502, detail='LLM 调用失败：' + str(exc))
    print(f"[llm-search] region={region} base={llm_base} choices={len(outer.get('choices') or [])} error={outer.get('error')}", flush=True)
    choices = outer.get('choices') or []
    if not choices:
        err = (outer.get('error') or {})
        detail = err.get('message') if isinstance(err, dict) else str(err)
        raise HTTPException(status_code=502, detail=f'LLM API 无响应（region={region}）：{detail or "choices 为空；请检查 stepfun.env 的 STEPFUN_REGION 是否与 key 区域一致"}')
    msg0 = (choices[0].get('message') or {})
    content = msg0.get('content') or msg0.get('reasoning_content') or msg0.get('output_text') or ''
    print(f"[llm-search] msg_keys={list(msg0.keys())} content_len={len(content or '')}", flush=True)
    if not content.strip():
        raise HTTPException(status_code=502, detail=f'LLM 返回空内容（choices=1 但无文本字段；可能模型拒答或字段不同）。模型返回字段：{list(msg0.keys())}。请在后端日志查看 [llm-search] msg_keys 行。')
    raw_text = content.strip()
    if raw_text.startswith('```'):
        raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text)
    queries = []
    try:
        queries = json.loads(raw_text)
    except Exception:
        for arr in reversed(re.findall(r'\[[^\]]*\]', raw_text)):
            try:
                parsed = json.loads(arr)
                if isinstance(parsed, list):
                    queries = parsed
                    break
            except Exception:
                continue
    if not isinstance(queries, list):
        queries = []
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()][:3]
    if not queries:
        return {'items': [], 'raw': content,
                'parse_hint': 'LLM 未生成有效搜索词。原始回复：' + (content.strip()[:300] or '空'),
                'results': []}
    repos, seen, warnings = [], set(), []
    for q in queries:
        try:
            found = search_skill_repositories('agent skills ' + q, limit=4)
        except DiscoveryError as exc:
            warnings.append(str(exc))
            continue
        for r in found:
            if r not in seen:
                seen.add(r)
                repos.append(r)
    if not repos:
        return {'items': [], 'raw': content,
                'parse_hint': 'GitHub 搜索未找到匹配仓库（网络受限或无结果）。可换任务描述再试。',
                'results': [], 'warnings': warnings}
    scan_terms = []
    for q in queries:
        for w in re.findall(r'[a-z0-9][a-z0-9_-]{2,31}', q.lower()):
            if w not in scan_terms:
                scan_terms.append(w)
    scan = discover_skills_from_repos(repos[:6], terms=scan_terms[:6], limit=10)
    results = scan['results']
    warnings += scan['warnings']
    if not results:
        return {'items': [], 'raw': content,
                'parse_hint': '找到仓库但未识别出 SKILL.md 候选（可换任务描述再试）。',
                'results': [], 'warnings': warnings}
    return {'items': results, 'raw': content, 'parse_hint': '', 'results': results,
            'warnings': warnings,
            'trust_note': '候选来自 GitHub 真实仓库（LLM 生成搜索词 + GitHub API 搜索）；下载与运行仍须完整安检'}



def llm_chat(req: ChatRequest):
    api_key = os.environ.get('STEPFUN_API_KEY', '')
    if not api_key:
        raise HTTPException(status_code=500, detail='STEPFUN_API_KEY is not set')
    payload = {
        'model': req.model or 'step-3.7-flash',
        'messages': [{'role': m.role, 'content': m.content} for m in req.messages],
        'stream': bool(req.stream),
        'temperature': float(req.temperature),
        'max_tokens': int(req.max_tokens),
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req_obj = urllib.request.Request(
        'https://api.stepfun.com/v1/chat/completions',
        data=data,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req_obj, timeout=60) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        raise HTTPException(status_code=e.code, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return ChatResponse(
        id=body.get('id', uuid.uuid4().hex),
        created=body.get('created', int(datetime.now().timestamp())),
        model=body.get('model', payload['model']),
        choices=body.get('choices', []),
        usage=ChatUsage(**body.get('usage', {})),
    )

@app.get('/api/environment/status')
def environment_status():
    ports = [3030, 8200, 11435, 8888, 13200, 8000]
    checks = []
    for port in ports:
        listening = _is_port_listening(port)
        checks.append({'port': port, 'listening': listening, 'service': _service_name(port)})
    return {'time': _now(), 'checks': checks}

def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def _save_report(skill_name: str, report: dict, repaired: bool = False):
    report['time'] = _now()
    report['repaired'] = repaired
    (REPORTS_DIR / f'{skill_name}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

def _health_check(skill_dir: Path, integrity_verified: bool = False) -> dict:
    return evaluate_skill_health(skill_dir, integrity_verified=integrity_verified)


def _mock_scan(skill_dir: Path) -> bool:
    """兼容旧调用名；返回基础静态扫描结果，不代表完整安全检测。"""
    from governance.checks import check_static_scan
    return check_static_scan(skill_dir)[0]

def _infer_use(name: str) -> str:
    return {
        'tao-generate-image-grounding': '开放词汇目标定位',
        'tao-generate-referring-expressions': '区域描述与框核验',
        'rag-blueprint': '企业内部知识库',
        'skill-doctor': 'Skill 健康诊断',
        'local-pedestrian-detector': '行人检测组合 Skill',
        'sample-skill-broken': '损坏 Skill 样本',
    }.get(name, '通用 Skill')

def _is_port_listening(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False

def _service_name(port: int) -> str:
    return {3030: 'OpenClaw', 8200: 'ComfyUI', 11435: 'Ollama', 8888: 'Jupyter', 13200: 'OpenClaw Alt', 8000: 'SkillPulse API'}.get(port, 'Unknown')
AUDIT_PATH = REPORTS_DIR / "audit.jsonl"
AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
VERSIONS_DIR = REPORTS_DIR.parent / "versions"
VERSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _append_audit(entry: dict) -> None:
    entry.setdefault('run_id', entry.get('run_id'))
    if CURRENT_ACTOR.get() is not None:
        entry.setdefault('actor_id', CURRENT_ACTOR.get())
    try:
        append_chained(AUDIT_PATH.with_name('audit-chain.jsonl'), entry)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f'审计记录无法写入: {exc}') from exc


def _require_audit_integrity() -> None:
    result = verify_chain(AUDIT_PATH.with_name('audit-chain.jsonl'))
    if not result['valid']:
        raise HTTPException(status_code=409, detail='审计链已损坏，拒绝操作: ' + result['error'])


@app.post('/api/audit/logs')
def append_audit_log(entry: OperationAuditRequest):
    if entry.action not in {'add', 'remove', 'connect'}:
        raise HTTPException(status_code=422, detail='仅允许固定的操作审计动作')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', entry.target):
        raise HTTPException(status_code=422, detail='操作目标格式无效')
    if entry.node_id is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', entry.node_id):
        raise HTTPException(status_code=422, detail='节点 ID 格式无效')
    _append_audit({'time': _now(), 'action': 'operation-' + entry.action,
                   'target': entry.target, 'result': 'recorded', 'detail': None,
                   'node_id': entry.node_id, 'run_id': None})
    return {'saved': True}


def _read_audit_records():
    logs = []
    if AUDIT_PATH.exists():
        for line in AUDIT_PATH.read_text(encoding='utf-8', errors='ignore').splitlines():
            try:
                old = json.loads(line)
                if not isinstance(old, dict):
                    continue
                target = old.get('target')
                old_time = old.get('time')
                logs.append({'time': old_time if isinstance(old_time, str) and re.fullmatch(r'[0-9T:Z+ .-]{10,35}', old_time) else None,
                             'action': old.get('action') if old.get('action') in {'run', 'run-node', 'link', 'download', 'add', 'remove'} else 'legacy',
                             'target': target if isinstance(target, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', target) else 'legacy-entry',
                             'result': '旧记录未验证', 'detail': '原始内容已隐藏，防止旧日志泄露敏感输入',
                             'run_id': old.get('run_id') if isinstance(old.get('run_id'), str) and re.fullmatch(r'[0-9a-f]{8}', old['run_id']) else None,
                             'integrity': 'legacy_unverified'})
            except Exception:
                pass
    chain = verify_chain(AUDIT_PATH.with_name('audit-chain.jsonl'))
    if not chain['valid']:
        raise HTTPException(status_code=409, detail='审计链校验失败: ' + chain['error'])
    logs.extend({**record, 'integrity': 'local_hash_chain'} for record in chain['records'])
    return logs, chain


@app.get('/api/audit/logs')
def get_audit_logs(limit: int = 200):
    limit = max(1, min(limit, 1000))
    logs, chain = _read_audit_records()
    return {'logs': logs[-limit:], 'integrity': {'valid': True, 'chained_count': chain['count'],
            'legacy_count': len(logs) - chain['count'],
            'limitation': '本地哈希链可发现内容修改/重排，不能防止能重写全部文件者删除链尾'}}


@app.get('/api/audit/export')
def export_audit():
    logs, _ = _read_audit_records()
    content = ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in logs)
    return Response(content, media_type='application/x-ndjson', headers={'Content-Disposition': 'attachment; filename="skillpulse-audit.jsonl"'})


@app.get('/api/audit/verify')
def audit_verify():
    chain = verify_chain(AUDIT_PATH.with_name('audit-chain.jsonl'))
    return {'valid': chain['valid'], 'count': chain['count'], 'last_hash': chain['last_hash'],
            'error': chain.get('error'), 'scope': chain['scope'],
            'legacy_records_verified': False,
            'limitation': '本地哈希链不能防止有文件写权限者删除尾部或重建整条链；没有外部锚定'}


@app.get('/api/audit/report/{run_id}')
def audit_report(run_id: str):
    if not re.fullmatch(r'[0-9a-f]{8}', run_id):
        raise HTTPException(status_code=400, detail='Invalid run_id')
    actor_id = CURRENT_ACTOR.get()
    path = (USER_RUNS_DIR / str(actor_id) if actor_id is not None else CATALOG_DIR) / f'pipeline-{run_id}.json'
    if not path.is_file():
        raise HTTPException(status_code=404, detail='Run record not found')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('run_id') != run_id:
        raise HTTPException(status_code=409, detail='运行摘要中的 run_id 不匹配')
    chain = verify_chain(AUDIT_PATH.with_name('audit-chain.jsonl'))
    if not chain['valid']:
        raise HTTPException(status_code=409, detail='审计链校验失败: ' + chain['error'])
    chained_events = [record for record in chain['records'] if record.get('run_id') == run_id]
    commit = next((record for record in reversed(chained_events) if record.get('action') == 'run' and record.get('target') == 'pipeline'), None)
    expected_digest = None
    if commit:
        try:
            expected_digest = json.loads(commit.get('detail') or '{}').get('record_sha256')
        except (TypeError, ValueError):
            expected_digest = None
    actual_digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if expected_digest and expected_digest != actual_digest:
        raise HTTPException(status_code=409, detail='运行摘要与审计链中的内容哈希不一致')
    report = {
        'kind': 'SkillPulse local audit report prototype',
        'run_id': run_id,
        'started_at': data.get('started_at'), 'finished_at': data.get('finished_at'),
        'passed': data.get('response_summary', {}).get('passed'),
        'nodes': data.get('response_summary', {}).get('nodes', []),
        'edges': data.get('response_summary', {}).get('edges', []),
        'request_summary': data.get('request_summary', {}),
        'audit_integrity': {'chained_events': len(chained_events), 'chain_valid_now': True,
                            'run_record_verified': expected_digest == actual_digest,
                            'coverage': 'current_chain' if expected_digest == actual_digest else 'legacy_unverified'},
        'limitations': ['仅含本地检查和哈希摘要，不是第三方合规认证',
                        '本地哈希清单不是公钥数字签名', '子进程执行器不是安全沙箱',
                        '本地审计哈希链没有外部锚定，不能防止整链重写或链尾删除'],
    }
    return Response(json.dumps(report, ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="skillpulse-audit-{run_id}.json"'})


@app.post('/api/compatibility/{skill_name}')
def compatibility(skill_name: str, req: CompatibilityRequest):
    return _dependency_compatibility([skill_name])


class PipelineCompatibilityRequest(BaseModel):
    skill_names: List[str]


def _dependency_compatibility(names: List[str]) -> dict:
    """仅比较声明的精确依赖版本；不声称已检测系统中已安装的包。"""
    from collections import defaultdict
    declared = defaultdict(list)
    skills = []
    notes = []
    for name in dict.fromkeys(names):
        report = _health_check(_skill_dir(name))
        schema = report.get('schema')
        if not schema:
            raise HTTPException(status_code=422, detail=f'{name} 缺少可用 Schema')
        resources = schema['resources']
        skills.append({'name': name, 'version': schema['version'], 'status': report['status'], 'resources': resources})
        if report['status'] != 'HEALTHY':
            notes.append(f'{name} 未通过安检')
        for declaration in resources.get('dependencies', []):
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)(?:==([A-Za-z0-9_.-]+))?', declaration)
            if not match:
                notes.append(f'{name} 的依赖声明暂不支持自动判断: {declaration}')
                continue
            declared[match.group(1).lower().replace('_', '-')].append((name, match.group(2)))
    for package, items in declared.items():
        pins = {pin for _, pin in items if pin}
        if len(pins) > 1:
            notes.append(f'{package} 存在冲突的精确版本要求: {", ".join(sorted(pins))}')
    return {'compatible': not notes, 'notes': notes, 'skills': skills,
            'scope': '仅比较 Skill Schema 声明的精确依赖版本与安检状态；未验证本机安装包、驱动或模型兼容性'}


@app.post('/api/pipelines/compatibility')
def pipeline_compatibility(req: PipelineCompatibilityRequest):
    if not req.skill_names:
        raise HTTPException(status_code=422, detail='至少提供一个 Skill')
    return _dependency_compatibility(req.skill_names)


class PipelineExportRequest(BaseModel):
    nodes: List[LegoNode]
    edges: List[LegoEdge]
    format: Optional[str] = 'yaml'
    generated_at: Optional[str] = None


@app.post('/api/pipelines/export')
def pipeline_export(req: PipelineExportRequest):
    if not req.nodes:
        raise HTTPException(status_code=422, detail='不能导出空流水线')
    nodes = []
    for node in req.nodes:
        if node.control_type in CONTROL_SCHEMAS:
            if node.name != node.control_type:
                raise HTTPException(status_code=422, detail='控制节点名称与类型不一致')
            version = CONTROL_SCHEMAS[node.control_type]['version']
        elif node.control_type == 'skill':
            report = _health_check(_skill_dir(node.name))
            if report['status'] != 'HEALTHY':
                raise HTTPException(status_code=403, detail=f'未通过安检的 Skill 不能导出为可运行流水线: {node.name}')
            version = report['version']
        else:
            raise HTTPException(status_code=422, detail=f'不支持导出节点类型: {node.control_type}')
        nodes.append({'id': node.id, 'name': node.name, 'version': version, 'control_type': node.control_type})
    ids = {node['id'] for node in nodes}
    if len(ids) != len(nodes):
        raise HTTPException(status_code=422, detail='重复节点 ID')
    node_types = {node['id']: node['control_type'] for node in nodes}
    edges = []
    for edge in req.edges:
        if edge.from_ not in ids or edge.to not in ids:
            raise HTTPException(status_code=422, detail='导出连线引用不存在的节点')
        if edge.from_ == edge.to:
            raise HTTPException(status_code=422, detail='不能导出自连节点')
        if node_types[edge.from_] == 'if_branch' and edge.route not in ('true', 'false'):
            raise HTTPException(status_code=422, detail='分支导出缺少 true/false 路由')
        if node_types[edge.from_] != 'if_branch' and edge.route is not None:
            raise HTTPException(status_code=422, detail='非分支节点不能指定路由')
        edges.append({'from': edge.from_, 'to': edge.to, 'route': edge.route})
    if len({(edge['from'], edge['to'], edge['route']) for edge in edges}) != len(edges):
        raise HTTPException(status_code=422, detail='不能导出重复连线')
    for node in nodes:
        if node['control_type'] == 'if_branch' and sorted(edge['route'] for edge in edges if edge['from'] == node['id']) != ['false', 'true']:
            raise HTTPException(status_code=422, detail='分支必须同时连接 true 与 false 出线')
    indegree = {node_id: 0 for node_id in ids}
    adjacency = {node_id: [] for node_id in ids}
    for edge in edges:
        adjacency[edge['from']].append(edge['to'])
        indegree[edge['to']] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node_id = ready.pop()
        visited += 1
        for target in adjacency[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(ids):
        raise HTTPException(status_code=422, detail='不能导出包含循环依赖的拓扑')
    payload = {'format_version': 1, 'generated_at': req.generated_at or _now(), 'nodes': nodes, 'edges': edges,
               'note': '拓扑与版本快照；不包含运行输入、凭证或 Skill 源代码'}
    yaml_lines = ['format_version: 1', 'generated_at: ' + json.dumps(payload['generated_at']), 'nodes:']
    for node in nodes:
        yaml_lines += ['  - id: ' + json.dumps(node['id']), '    name: ' + json.dumps(node['name']),
                       '    version: ' + json.dumps(node['version']), '    control_type: ' + json.dumps(node['control_type'])]
    yaml_lines.append('edges:')
    for edge in edges:
        yaml_lines += ['  - from: ' + json.dumps(edge['from']), '    to: ' + json.dumps(edge['to']),
                       '    route: ' + (json.dumps(edge['route']) if edge['route'] else 'null')]
    yaml_lines.append('note: ' + json.dumps(payload['note'], ensure_ascii=False))
    yaml_text = '\n'.join(yaml_lines) + '\n'
    openapi = {
        'openapi': '3.0.3',
        'info': {'title': 'SkillPulse Pipeline API prototype', 'version': '0.4.0',
                 'description': '本地运行请求描述，不包含鉴权或生产环境服务定义'},
        'paths': {'/api/pipelines/run': {'post': {
            'summary': 'Run a local governed pipeline',
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                'type': 'object', 'required': ['nodes', 'edges'],
                'properties': {'nodes': {'type': 'array', 'items': {'type': 'object'}},
                               'edges': {'type': 'array', 'items': {'type': 'object'}},
                               'inputs': {'type': 'object'}}}}}},
            'responses': {'200': {'description': 'Run result'}, '403': {'description': 'Skill failed governance gate'},
                          '422': {'description': 'Invalid topology or inputs'}}}}}
    }
    if req.format not in ('yaml', 'openapi'):
        raise HTTPException(status_code=422, detail='仅支持 yaml 或 openapi 导出')
    _append_audit({'time': _now(), 'action': 'operation-export', 'target': 'pipeline',
                   'result': 'recorded', 'detail': req.format, 'run_id': None})
    return {'format': req.format, 'yaml': yaml_text, 'openapi': openapi, 'payload': payload}


class CompositeSkillExportRequest(BaseModel):
    """复合 Skill 包导出请求；name 为生成的复合 Skill 名。"""
    name: str
    nodes: List[LegoNode]
    edges: List[LegoEdge]
    generated_at: Optional[str] = None


@app.post('/api/pipelines/export-skill')
def pipeline_export_composite(req: CompositeSkillExportRequest):
    """把画布编排导出为可复用复合 Skill 资产包。

    复合 Skill 只保存对原始子 Skill 的引用（名称、版本、Schema 摘要），
    不拷贝原始 SKILL.md / scripts 本体，也不修改任何原始 Skill，从而保留
    原始 Skill 的签名与完整性证据。仅允许已通过安检(HEALTHY)的 Skill 进入。
    """
    name = req.name.strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name):
        raise HTTPException(status_code=400, detail='复合 Skill 名须为 1\u201364 个字母数字/._- 字符')
    if not req.nodes:
        raise HTTPException(status_code=422, detail='不能导出空复合 Skill')

    # 复用 pipeline_export 的完整拓扑 / 健康 / 契约校验
    payload = pipeline_export(PipelineExportRequest(
        nodes=req.nodes, edges=req.edges, format='yaml', generated_at=req.generated_at))['payload']

    skill_nodes = [n for n in payload['nodes'] if n['control_type'] == 'skill']
    if not skill_nodes:
        raise HTTPException(status_code=422, detail='复合 Skill 至少包含一个已通过安检的 Skill 节点')

    # 每个子 Skill 只读引用：名称、版本、Schema 摘要、治理状态；不拷贝正文
    references = []
    for node in skill_nodes:
        report = _health_check(_skill_dir(node['name']))
        references.append({
            'skill_name': node['name'],
            'version': node['version'],
            'integrity_verified': report['integrity_verified'],
            'status': report['status'],
            'risk': report['risk'],
            'note': '只引用，不拷贝原始 SKILL.md/scripts；原始签名与完整性保持不变',
        })

    composite = {
        'format_version': 1,
        'skill_type': 'composite',
        'name': name,
        'description': '由 SkillPulse 画布编排生成的复合 Skill，按输入输出契约串联多个已通过可信治理的子 Skill。',
        'generated_at': payload['generated_at'],
        'pipeline': {'nodes': payload['nodes'], 'edges': payload['edges']},
        'references': references,
        'note': payload['note'],
    }
    _append_audit({'time': _now(), 'action': 'operation-export-skill', 'target': name,
                   'result': 'recorded', 'detail': f'复合 Skill，引用 {len(references)} 个子 Skill', 'run_id': None})
    return {'name': name, 'composite_skill': composite}


@app.get('/api/gateway/skill')
def gateway_skill():
    """生成 gateway.skill：function-call 类型的调用契约 manifest。

    外部 Agent 加载该 manifest 后，可把整套 SkillPulse 中台当作一个工具 Skill 来
    调用（检索 / 安检 / 编排 / 审计）。它只是调用契约描述，本身不执行任何业务，
    也不含凭据；对应 endpoint 需由部署方配置并另行鉴权。最终 Skill 业务执行仍由
    外部 Agent/Harness 完成，本平台不承担执行职责。
    """
    base = os.environ.get('SKILLPULSE_API_BASE', 'http://127.0.0.1:8000')
    manifest = {
        'skill_id': 'skillpulse-gateway',
        'name': 'SkillPulse Skill 管理中台',
        'skill_type': 'function_call',
        'version': '0.4.0',
        'description': ('作为 Agent 的工具，可检索可复用 Skill、执行五项可信治理安检、'
                        '编排并运行受治理流水线、导出复合 Skill，并校验审计链。'
                        'Agent 加载本 manifest 后以 function-call 方式调用 SkillPulse 中台接口；'
                        '中台本身不执行 Skill 业务，最终执行仍由 Agent/Harness 完成。'),
        'use_when': ['需要检索或安检 Agent Skill', '需要把多个 Skill 编排为复合 Skill', '需要可追溯的可信审计证据'],
        'not_for': ['直接运行第三方 Skill 业务逻辑', '代替 Agent Harness 执行 Skill'],
        'endpoint': {'base_url': base, 'auth_note': '演示为本地单机会话鉴权；生产需另行配置真实鉴权与传输加密'},
        'progressive_disclosure': {
            'meta': ['name', 'description', 'use_when', 'skill_type', 'endpoint.base_url'],
            'full': ['functions 契约与各接口参数 schema；命中任务后再加载对应函数描述'],
        },
        'functions': [
            {'name': 'search_skills', 'description': '按需求检索本地 Skill 及在线候选',
             'http': {'method': 'POST', 'path': '/api/search/skills', 'content_type': 'application/json'}},
            {'name': 'skill_health', 'description': '对单个 Skill 执行五项可信治理安检，返回健康报告',
             'http': {'method': 'GET', 'path': '/api/skills/{skill_name}/health'}},
            {'name': 'run_pipeline', 'description': '按拓扑运行受治理流水线',
             'http': {'method': 'POST', 'path': '/api/pipelines/run', 'content_type': 'application/json'}},
            {'name': 'export_pipeline', 'description': '导出流水线 YAML / OpenAPI 或复合 Skill 包',
             'http': {'method': 'POST', 'path': '/api/pipelines/export', 'content_type': 'application/json'}},
            {'name': 'compatibility_check', 'description': '检查 Skill 与目标运行环境的兼容性',
             'http': {'method': 'POST', 'path': '/api/compatibility/{skill_name}'}},
            {'name': 'verify_audit', 'description': '校验审计哈希链完整性',
             'http': {'method': 'GET', 'path': '/api/audit/verify'}},
        ],
        'note': '调用契约描述，不含业务实现；SKILLPULSE_API_BASE 环境变量可覆盖 base_url。',
    }
    _append_audit({'time': _now(), 'action': 'operation-export-gateway', 'target': 'skillpulse-gateway',
                   'result': 'recorded', 'detail': 'function-call 类型调用契约', 'run_id': None})
    return manifest


class WorkflowSaveRequest(BaseModel):
    name: str
    nodes: List[LegoNode]
    edges: List[LegoEdge]


@app.post('/api/me/workflows')
def save_my_workflow(request: Request, workflow: WorkflowSaveRequest):
    """复用完整拓扑/健康验证，仅保存无输入值的流程快照。"""
    if not 1 <= len(workflow.name.strip()) <= 80:
        raise HTTPException(status_code=422, detail='流程名称须为 1–80 字')
    skill_names = [node.name for node in workflow.nodes if node.control_type == 'skill']
    if len(skill_names) != len(set(skill_names)):
        raise HTTPException(status_code=422, detail='同一流程不能保存重复 Skill 节点')
    exported = pipeline_export(PipelineExportRequest(nodes=workflow.nodes, edges=workflow.edges, format='yaml'))
    record = persist_workflow(USER_WORKFLOWS_DIR, request.state.user['id'], workflow.name, exported['payload'])
    _append_audit({'time': _now(), 'action': 'workflow-save', 'target': record['id'],
                   'result': 'recorded', 'detail': '仅保存拓扑与版本', 'run_id': None})
    return record


@app.get('/api/me/workflows')
def my_workflows(request: Request):
    return list_workflows(USER_WORKFLOWS_DIR, request.state.user['id'])


@app.get('/api/me/workflows/{workflow_id}')
def my_workflow(request: Request, workflow_id: str):
    record = read_workflow(USER_WORKFLOWS_DIR, request.state.user['id'], workflow_id)
    if record is None:
        raise HTTPException(status_code=404, detail='当前账号没有此工作流')
    return record


@app.get('/api/me/runs')
def my_runs(request: Request):
    return list_runs(USER_RUNS_DIR, request.state.user['id'])


@app.get('/api/me/runs/{run_id}')
def my_run(request: Request, run_id: str):
    record = read_run(USER_RUNS_DIR, request.state.user['id'], run_id)
    if record is None:
        raise HTTPException(status_code=404, detail='当前账号没有此运行摘要')
    return record

@app.get('/api/compatibility/summary')
def compatibility_summary():
    summaries = []
    for path in SKILLS_DIR.iterdir():
        if not path.is_dir():
            continue
        try:
            summaries.append({'name': path.name, **_dependency_compatibility([path.name])})
        except HTTPException as exc:
            summaries.append({'name': path.name, 'compatible': False, 'notes': [str(exc.detail)]})
    return {'summary': summaries}


@app.get('/api/skills/{skill_name}/versions')
def skill_versions(skill_name: str):
    skill_dir = _skill_dir(skill_name)
    report = _health_check(skill_dir)
    schema = report.get('schema')
    current = {'version': schema['version'], 'status': 'current', 'source': 'disk',
               'schema_sha256': hashlib.sha256((skill_dir / 'skill.schema.json').read_bytes()).hexdigest()} if schema else None
    path = VERSIONS_DIR / (skill_name + '.json')
    history = []
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            for record in data.get('versions', []):
                if not isinstance(record, dict):
                    continue
                if record.get('snapshot_sha256'):
                    verified = verify_snapshot(VERSIONS_DIR, skill_name, record)
                    history.append({**record, 'status': 'snapshot_verified' if verified else 'snapshot_missing_or_tampered',
                                    'source': 'local_archive'})
                else:
                    history.append({**record, 'status': 'unverified_record', 'source': 'historical_record'})
    except (OSError, ValueError, TypeError):
        history = []
    return {'skill': skill_name, 'current': current, 'history': history,
            'note': '只有 snapshot_verified 有本地源码 ZIP 及哈希证据；旧记录仍不证明对应源码存在，快照哈希不是发布者签名'}


@app.post('/api/skills/{skill_name}/versions')
def save_skill_version(skill_name: str, payload: Dict[str, Any]):
    _require_audit_integrity()
    skill_dir = _skill_dir(skill_name)
    report = _health_check(skill_dir)
    if report['status'] != 'HEALTHY':
        raise HTTPException(status_code=403, detail='当前 Skill 未通过安检，不允许生成可信快照')
    current_info = skill_versions(skill_name)['current']
    if not current_info:
        raise HTTPException(status_code=422, detail='缺少可验证的当前版本')
    if payload.get('version') != current_info['version']:
        raise HTTPException(status_code=409, detail='不能凭文本创建版本：请求版本必须与磁盘 Skill Schema 一致')
    path = VERSIONS_DIR / (skill_name + '.json')
    current = {'versions': []}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=f'现有版本记录不可解析，未覆盖: {exc}') from exc
    versions = current.get('versions', [])
    if not isinstance(versions, list):
        raise HTTPException(status_code=409, detail='现有版本记录格式错误，未覆盖')
    try:
        archive, digest, manifest = build_snapshot(skill_dir)
        store_snapshot(VERSIONS_DIR, skill_name, current_info['version'], archive, digest)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f'快照未创建: {exc}') from exc
    if not any(v.get('snapshot_sha256') == digest for v in versions if isinstance(v, dict)):
        versions.append({'version': current_info['version'], 'schema_sha256': current_info['schema_sha256'],
                         'snapshot_sha256': digest, 'files': manifest, 'observed_at': _now(), 'status': 'snapshot'})
    current['versions'] = versions
    temp_path = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()  # 仅清理本次创建的临时元数据文件。
    _append_audit({'time': _now(), 'action': 'operation-snapshot', 'target': skill_name,
                   'result': 'recorded', 'detail': digest, 'run_id': None})
    return {'saved': True, 'skill': skill_name, 'version': current_info['version'],
            'schema_sha256': current_info['schema_sha256'], 'snapshot_sha256': digest,
            'files': len(manifest)}


@app.get('/api/skills/{skill_name}/snapshots/{digest}')
def download_skill_snapshot(skill_name: str, digest: str):
    skill_dir = _skill_dir(skill_name)
    if _health_check(skill_dir)['status'] != 'HEALTHY':
        raise HTTPException(status_code=403, detail='当前 Skill 未通过安检，禁止下载历史快照')
    record = next((item for item in skill_versions(skill_name)['history']
                   if item.get('snapshot_sha256') == digest and item.get('status') == 'snapshot_verified'), None)
    if record is None:
        raise HTTPException(status_code=404, detail='已验证快照不存在')
    path = snapshot_path(VERSIONS_DIR, skill_name, record['version'], digest)
    _append_audit({'time': _now(), 'action': 'operation-snapshot-download', 'target': skill_name,
                   'result': 'recorded', 'detail': digest, 'run_id': None})
    return Response(path.read_bytes(), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{path.name}"'})
