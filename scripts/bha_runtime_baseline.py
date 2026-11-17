"""Apply verified BHA baseline overlays without changing Pipeline main.

Each archive retains the original repository baseline and latest accepted value.
A changed repository value is accepted only if it is one of those two identities.
"""
import copy
import re

SHA = re.compile(r'[0-9a-f]{40}\Z')


def validate_inputs(value):
    if (not isinstance(value, dict) or set(value) != {'main','config','ocr_cache','ocr_patch','helper','patch_layout'}
            or any(not isinstance(value[k], str) or not SHA.fullmatch(value[k])
                   for k in ('main','config','ocr_cache','ocr_patch','helper'))
            or not isinstance(value['patch_layout'],str) or not value['patch_layout']):
        raise ValueError('invalid baseline input identity')


def apply(inputs, trees, overlay):
    if (inputs.get('version') != 1 or set(inputs.get('archives',{})) != {str(i) for i in range(32)}
            or set(trees) != {str(i) for i in range(32)}
            or not isinstance(overlay,dict) or set(overlay) != {'kind','version','archives'}
            or overlay['kind'] != 'bha-verified-baseline-overlay' or overlay['version'] != 1
            or not isinstance(overlay['archives'],dict)):
        raise ValueError('invalid complete baseline or overlay')
    result_inputs=copy.deepcopy(inputs);result_trees=dict(trees)
    for key,row in overlay['archives'].items():
        if (key not in trees or not isinstance(row,dict)
                or set(row) != {'before_inputs','before_tree','inputs','tree','acceptance'}):
            raise ValueError('invalid baseline overlay archive')
        for name in ('before_inputs','inputs'):validate_inputs(row[name])
        for name in ('before_tree','tree'):
            if not isinstance(row[name],str) or not SHA.fullmatch(row[name]):raise ValueError('invalid baseline tree')
        current=(inputs['archives'][key],trees[key])
        if current not in [(row['before_inputs'],row['before_tree']),(row['inputs'],row['tree'])]:
            raise ValueError('repository baseline conflicts with verified overlay')
        if not isinstance(row['acceptance'],dict) or not row['acceptance']:
            raise ValueError('verified baseline acceptance missing')
        result_inputs['archives'][key]=copy.deepcopy(row['inputs']);result_trees[key]=row['tree']
    return result_inputs,result_trees


def advance(overlay, inputs, trees, *, archive, after_inputs, after_tree, acceptance):
    key=str(archive)
    if type(archive) is not int or not 0<=archive<32:raise ValueError('invalid archive')
    validate_inputs(after_inputs)
    if not isinstance(after_tree,str) or not SHA.fullmatch(after_tree):raise ValueError('invalid target tree')
    result=copy.deepcopy(overlay)
    previous=result['archives'].get(key)
    if previous and (inputs['archives'][key] != previous['inputs'] or trees[key] != previous['tree']):
        raise ValueError('effective baseline changed before advance')
    result['archives'][key]={'before_inputs': previous['before_inputs'] if previous else inputs['archives'][key],
        'before_tree':previous['before_tree'] if previous else trees[key],
        'inputs':copy.deepcopy(after_inputs),'tree':after_tree,'acceptance':acceptance}
    return result


def empty():return {'kind':'bha-verified-baseline-overlay','version':1,'archives':{}}
