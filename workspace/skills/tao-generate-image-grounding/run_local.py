SKILL_META = {'name':'tao-generate-image-grounding','version':'0.1.0','inputs':[{'name':'query','type':'string','required':True,'description':'目标描述'}],'outputs':[{'name':'boxes','type':'json','description':'检测框'},{'name':'image','type':'image','description':'可视化图片'}]}
def run(inputs):
    query = inputs.get('query') or inputs.get('input_1') or ''
    return {'ok': True, 'outputs': {'boxes': [{'label': query, 'score': 0.91}], 'image': 'mock://grounding.png'}, 'artifacts': ['mock://grounding.png']}
