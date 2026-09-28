SKILL_META = {'name':'local-pedestrian-detector','version':'0.1.0','inputs':[{'name':'query','type':'string','required':True,'description':'检测请求'}],'outputs':[{'name':'boxes','type':'json','description':'行人框'},{'name':'image','type':'image','description':'可视化图片'}]}
def run(inputs):
    query = inputs.get('query') or inputs.get('input_1') or ''
    return {'ok': True, 'outputs': {'boxes': [{'label': 'pedestrian', 'score': 0.78}], 'image': 'mock://pedestrian.png'}, 'artifacts': ['mock://pedestrian.png']}
