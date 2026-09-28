"""将真实检查结果格式化为报告；不制造不存在的合规结论。"""
import json


def run(inputs):
    raw = inputs.get('result')
    if not isinstance(raw, str) or not raw.strip():
        return {'ok': False, 'error': '缺少 skill-doctor 的 result 文本', 'outputs': {}, 'artifacts': []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {'ok': False, 'error': 'result 不是有效的检查结果 JSON', 'outputs': {}, 'artifacts': []}
    if not isinstance(data, dict) or not isinstance(data.get('checks'), list) or not data.get('skill'):
        return {'ok': False, 'error': 'result 缺少 Skill 名称或检查明细', 'outputs': {}, 'artifacts': []}
    names = {item.get('name') for item in data['checks'] if isinstance(item, dict)}
    if names != {'Catalog', 'Scanned', 'Signed', 'Evaluated', 'Documented'}:
        return {'ok': False, 'error': '检查明细不是完整的五项结果', 'outputs': {}, 'artifacts': []}
    lines = [
        f"# SkillPulse 本地检查报告：{data['skill']}",
        '', f"版本：{data.get('version') or '未知'}",
        f"结论：{data.get('status') or 'UNKNOWN'} · 风险：{data.get('risk') or 'UNKNOWN'}",
        '上游身份：未验证 · 数字签名：未验证 · 本地完整性：' + ('通过' if data.get('integrity_verified') else '未通过'),
        '', '## 五项检查',
    ]
    for item in data['checks']:
        lines.append(f"- {item['name']}：{item.get('status', 'UNKNOWN')} — {item.get('detail', '')}")
    lines.extend([
        '', '## 适用边界',
        '- 这是本地检查证据，不是第三方合规认证。',
        '- 完整性检查使用仓库本地 SHA-256 清单，不是公钥数字签名。',
        '- 样例测试只证明声明的 smoke case 运行结果；部分示例能力仍是 mock。',
    ])
    return {'ok': True, 'outputs': {'report': '\n'.join(lines)}, 'artifacts': []}
