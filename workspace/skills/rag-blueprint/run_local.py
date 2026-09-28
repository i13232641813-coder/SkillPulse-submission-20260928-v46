SKILL_META = {'name':'rag-blueprint','version':'0.1.0','inputs':[{'name':'query','type':'string','required':True,'description':'检索问题'},{'name':'context','type':'json','required':False,'description':'额外上下文'}],'outputs':[{'name':'answer','type':'text','description':'回答'}]}
def run(inputs):
    query = inputs.get('query') or inputs.get('input_1') or ''
    context = inputs.get('context') or {}
    return {'ok': True, 'outputs': {'answer': f'[rag-blueprint] 已基于 {len(context) if isinstance(context, dict) else 1} 条上下文回答问题：{query}'}, 'artifacts': []}
