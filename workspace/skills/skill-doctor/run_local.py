"""对仓库中指定的本地 Skill 执行实际五项治理检查。"""
import json
import re
import sys
from pathlib import Path


SKILLS_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = Path(__file__).resolve().parents[3] / 'backend'


def run(inputs):
    target = str(inputs.get('query') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', target):
        return {'ok': False, 'error': '请输入本地 Skill 的准确目录名', 'outputs': {}, 'artifacts': []}
    target_dir = (SKILLS_DIR / target).resolve()
    if target_dir.parent != SKILLS_DIR or not target_dir.is_dir():
        return {'ok': False, 'error': '本地 Skill 不存在', 'outputs': {}, 'artifacts': []}
    if target == 'skill-doctor':
        return {'ok': False, 'error': '不能在运行器中自检 skill-doctor；请使用平台安检入口', 'outputs': {}, 'artifacts': []}
    sys.path.insert(0, str(BACKEND_DIR))
    from governance.checks import skill_health

    report = skill_health(target_dir)
    payload = {
        'skill': report['skill'], 'version': report.get('version'),
        'status': report['status'], 'risk': report['risk'], 'checks': report['checks'],
        'integrity_verified': report['integrity_verified'],
        'signature_verified': report['signature_verified'],
        'origin_verified': report['origin_verified'],
        'integrity_scope': '仓库本地 SHA-256 清单；不是公钥数字签名',
        'evaluation_scope': '项目内 smoke cases；部分现有 Skill 的任务运行器仍为 mock',
    }
    return {'ok': True, 'outputs': {'result': json.dumps(payload, ensure_ascii=False)}, 'artifacts': []}
