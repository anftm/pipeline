"""Synthetic legacy matrix for parser tests only; never used as routing config."""
def legacy_worker_fixture():
    owner = lambda name, low, high, repo=None: {
        'name':name, 'owner':('account-' + name), 'min_bytes':low, 'max_bytes':high,
        **({'repository':repo} if repo else {})}
    native = [owner('small', 0, 104857600, 'account-small/pdf-pipeline'),
              owner('large', 104857600, None)]
    native[0].update(name='native-under-100m',owner='vomebook',repository='vomebook/pdf-pipeline')
    native[1].update(name='native-100m-plus',owner='anftm')
    converted={'name':'converted','owner':'ambrossee768','repository':'ambrossee768/pdf-pipeline'}
    old=[{'name':'native-small','owner':'vomebook','repository':'vomebook/pdf-pipeline',
          'min_bytes':0,'max_bytes':5242880},
         {'name':'native-small-upper','owner':'rioholland79','min_bytes':5242880,'max_bytes':10485760},
         {'name':'native-small-mid','owner':'devondunn7','min_bytes':10485760,'max_bytes':52428800},
         {'name':'native-medium-lower','owner':'dellamcastillo','min_bytes':52428800,'max_bytes':104857600},
         {'name':'native-medium','owner':'anftm','min_bytes':104857600,'max_bytes':262144000},
         {'name':'native-medium-upper','owner':'brodievsalas','min_bytes':262144000,'max_bytes':524288000},
         {'name':'native-large','owner':'alicetran68','min_bytes':524288000,'max_bytes':None}]
    return {'version':1,'publisher_repository':'anftm/pipeline','native_lanes':native,
            'converted_lane':converted,'legacy_configs':[{'version':1,'publisher_repository':'anftm/pipeline',
                'native_lanes':old,'converted_lane':converted.copy()}]}
