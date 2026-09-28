SKILL_META = {'name':'sample-skill-broken','version':'0.1.0','inputs':[{'name':'query','type':'string','required':True,'description':'触发样本'}],'outputs':[{'name':'result','type':'text','description':'样本输出'}]}
def run(inputs):
    raise RuntimeError('sample skill is intentionally broken for demo')
