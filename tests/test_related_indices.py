import fnmatch
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import elk_core as core
import mcp_server as m

TZ = core.parse_tz('+08:00')
DETECT = '2117171084237459775'


def flat(src, prefix=''):
    out = {}
    for k, v in src.items():
        if isinstance(v, dict):
            out.update(flat(v, prefix + k + '.'))
        else:
            out[prefix + k] = v
    return out


class FakeEs:
    """只实现本模块用到的只读 API：_cat/indices、_field_caps、_search（bool/query_string/match_phrase/prefix/排序/size）。"""

    def __init__(self, docs):
        self.docs = docs
        self.paths = []

    def _indices(self, pattern):
        names = sorted({d['_index'] for d in self.docs})
        return [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in pattern.split(','))]

    def _match(self, q, src):
        f = flat(src)
        if 'bool' in q:
            b = q['bool']
            if not all(self._match(c, src) for c in b.get('must', []) + b.get('filter', [])):
                return False
            return not b.get('should') or any(self._match(c, src) for c in b['should'])
        if 'match_all' in q or 'range' in q:
            return True
        if 'match_phrase' in q:
            (field, value), = q['match_phrase'].items()
            return value.lower() in str(f.get(field, '')).lower()
        if 'prefix' in q:
            (field, value), = q['prefix'].items()
            return str(f.get(field[:-len('.keyword')], '')).startswith(value)
        if 'query_string' in q:
            text = q['query_string']['query']
            msg = str(f.get('message', ''))
            terms = [t.strip().strip('"') for t in text.split(' OR ')]
            return any(t in msg for t in terms)
        raise AssertionError(q)

    def __call__(self, env, method, path, body=None, interactive=True):
        self.paths.append(path)
        p = path.split('?')[0].strip('/')
        if p.startswith('_cat/indices/'):
            return [{'index': n} for n in self._indices(p[len('_cat/indices/'):])]
        idx, _, api = p.rpartition('/')
        docs = [d for d in self.docs if d['_index'] in self._indices(idx)]
        if api == '_field_caps':
            return {'fields': {k: {'keyword': {}} for d in docs for k in flat(d['_source'])}}
        assert api == '_search', path
        hits = [d for d in docs if self._match(body['query'], d['_source'])]
        if body.get('sort'):
            desc = list(body['sort'][0].values())[0]['order'] == 'desc'
            keys = [list(s)[0] for s in body['sort']]

            def key(d):
                f = flat(d['_source'])
                out = []
                for k in keys:
                    if k == '_doc':
                        out.append((0, self.docs.index(d)))
                    else:
                        v = f.get(k[:-len('.keyword')] if k.endswith('.keyword') else k)
                        out.append((1, '') if v is None else (0, v))  # missing 排在后面
                return out
            hits = sorted(hits, key=key, reverse=desc)
        return {'took': 1, 'hits': {'total': {'value': len(hits)}, 'hits': hits[:body.get('size', 10)]}}


def doc(index, i, message, ts, **extra):
    src = dict({'@timestamp': ts, 'message': message, 'host': {'name': extra.pop('host', 'svc-1')}}, **extra)
    return {'_index': index, '_id': '%s-%d' % (index, i), '_source': src}


def algo_docs(n_info=50):
    docs = [doc('beta-algo-2026-11-02', 0, '2026-11-02 08:24:56 - INFO FRC1 %s || [/process] 荧光检测 Get requst' % DETECT,
                '2026-11-02T08:25:02.563Z', host='fluo-pipeline-1')]
    for i in range(1, n_info + 1):
        docs.append(doc('beta-algo-2026-11-02', i, '2026-11-02 08:24:58 - INFO FRC1 %s || step %d' % (DETECT, i),
                        '2026-11-02T08:25:02.564Z', host='fluo-pipeline-1'))
    docs.append(doc('beta-algo-2026-11-02', 900, '2026-11-02 08:24:59 - ERROR FRC1 %s || 未找到默认机型的浓度配置' % DETECT,
                    '2026-11-02T08:25:02.566Z', host='fluo-pipeline-1'))
    docs.append(doc('beta-algo-2026-11-02', 901, '2026-11-02 08:24:59 - INFO FRC1 %s || pipeline over' % DETECT,
                    '2026-11-02T08:25:02.567Z', host='fluo-pipeline-1'))
    return docs


def software_docs():
    return [
        doc('beta-software-2026-11-02', 1, '【荧光算法请求参数】：{"detectId":%s,"deviceId":"FRC100124020600019"}' % DETECT,
            '2026-11-02T08:24:56.407Z', time='2026-11-02 16:24:56.407', level='INFO', traceId='t-1', host='core-1'),
        doc('beta-software-2026-11-02', 2, 'INSERT INTO test_data_detail VALUES (%s)' % DETECT,
            '2026-11-02T08:24:56.431Z', time='2026-11-02 16:24:56.431', level='INFO', traceId='t-1', host='core-1'),
        doc('beta-software-2026-11-02', 3, 'other', '2026-11-02T08:24:57.000Z',
            time='2026-11-02 16:24:57.000', level='INFO', traceId='t-2', host='core-1'),
    ]


def make_env(**extra):
    env = {'_name': 'test', 'index': 'beta-software-*', 'max_size': 500, 'timeout': 60,
           '_fields': dict(core.DEFAULT_FIELDS)}
    env.update(extra)
    return env


class RelatedIndices(unittest.TestCase):
    def setUp(self):
        core._RELATED_CACHE.clear()
        core._PROFILE_CACHE.clear()

    def run_with(self, docs, fn, *args, **kwargs):
        fake = FakeEs(docs)
        with patch.object(core, 'call', side_effect=fake):
            return fn(*args, **kwargs), fake

    # ---- 1. 排序回退
    def test_sort_falls_back_to_timestamp_and_doc_order(self):
        clauses = core.sort_clause(make_env(), desc=True)
        self.assertEqual([list(c)[0] for c in clauses], ['time.keyword', '@timestamp', '_doc'])
        self.assertTrue(all(list(c.values())[0]['order'] == 'desc' for c in clauses))
        self.assertEqual(clauses[1]['@timestamp']['unmapped_type'], 'date')

    def test_desc_returns_true_tail_for_index_without_time_field(self):
        out, _ = self.run_with(algo_docs(), core.op_search, make_env(),
                               {'index': 'beta-algo-*', 'query': '"%s"' % DETECT, 'size': 2, 'desc': True,
                                'since': '2026-11-02 16:00', 'until': '2026-11-02 17:00'}, TZ)
        lines = out.splitlines()
        self.assertIn('未找到默认机型的浓度配置', lines[-2])
        self.assertIn('pipeline over', lines[-1])

    # ---- 2. 关联索引发现、提示、自动改查、合并
    def test_discover_related_by_default_index_prefix(self):
        docs = software_docs() + algo_docs(1) + [
            doc('beta-nginx.2026.11.02', 1, 'x', '2026-11-02T00:00:00Z'),
            doc('.kibana', 1, 'x', '2026-11-02T00:00:00Z'),
            doc('sta-algo-2026-11-02', 1, 'x', '2026-11-02T00:00:00Z')]
        found, _ = self.run_with(docs, core.related_indices, make_env())
        self.assertEqual(found, {'algo': 'beta-algo-*', 'nginx': 'beta-nginx.*'})

    def test_related_config_explicit_and_off(self):
        found, fake = self.run_with([], core.related_indices, make_env(related={'algo': 'x-algo-*'}))
        self.assertEqual(found, {'algo': 'x-algo-*'})
        found, _ = self.run_with([], core.related_indices, make_env(related='off'))
        self.assertEqual(found, {})
        self.assertEqual(fake.paths, [])

    def test_hint_when_default_index_has_hits(self):
        out, _ = self.run_with(software_docs() + algo_docs(), core.op_search, make_env(),
                               {'query': '"%s"' % DETECT, 'since': '1d'}, TZ)
        self.assertIn('# env=test index=beta-software-* total=2', out)
        self.assertIn('关联索引命中：algo(beta-algo-*) 53 条', out)

    def test_auto_switch_when_default_index_empty(self):
        out, _ = self.run_with(software_docs() + algo_docs(), core.op_search, make_env(),
                               {'host': 'fluo-pipeline*', 'since': '1d', 'size': 5}, TZ)
        self.assertIn('默认索引 beta-software-* 无结果，已自动改查关联索引：algo(beta-algo-*) 53 条', out)
        self.assertIn('index=beta-algo-* total=53 shown=5', out)
        self.assertIn('荧光检测', out)

    def test_merge_mode_combines_by_timestamp(self):
        out, _ = self.run_with(software_docs() + algo_docs(1), core.op_search, make_env(),
                               {'query': '"%s"' % DETECT, 'since': '1d', 'related': 'merge'}, TZ)
        self.assertIn('index=beta-software-*,beta-algo-* total=6', out)
        body = out.splitlines()[2:]
        self.assertIn('荧光算法请求参数', body[0])
        self.assertIn('pipeline over', body[-1])

    def test_no_related_lookup_when_off_or_index_given_or_no_locator(self):
        for p in ({'query': DETECT, 'related': 'off'}, {'query': DETECT, 'index': 'beta-software-*'},
                  {'level': 'ERROR'}):
            core._RELATED_CACHE.clear()
            _, fake = self.run_with(software_docs() + algo_docs(1), core.op_search, make_env(),
                                    dict(p, since='1d'), TZ)
            self.assertFalse(any('beta-algo' in x or '_cat' in x for x in fake.paths), p)

    def test_index_accepts_related_name(self):
        out, _ = self.run_with(software_docs() + algo_docs(1), core.op_search, make_env(),
                               {'index': 'algo', 'query': 'pipeline', 'since': '1d'}, TZ)
        self.assertIn('index=beta-algo-* total=1', out)

    # ---- 3. 按索引的字段映射
    def test_missing_level_field_matches_message(self):
        out, _ = self.run_with(algo_docs(), core.op_search, make_env(),
                               {'index': 'algo', 'level': 'ERROR', 'since': '1d'}, TZ)
        self.assertIn('total=1', out)
        self.assertIn('改为在 message 中按短语匹配', out)
        self.assertRegex(out.splitlines()[-1], r'ERROR .*未找到默认机型')  # 级别列从消息中提取

    def test_index_fields_config_overrides(self):
        env, _ = self.run_with(algo_docs(1), core.env_for_index,
                               make_env(index_fields={'beta-algo-*': {'host': 'fields.pod_name'}}), 'beta-algo-*')
        self.assertEqual(env['_fields']['host'], 'fields.pod_name')
        self.assertEqual(env['_fields']['level'], core.MESSAGE_FIELD)
        default = make_env()
        self.assertIs(core.env_for_index(default, 'beta-software-*'), default)

    def test_build_query_message_field_clause(self):
        env = make_env()
        env['_fields']['level'] = core.MESSAGE_FIELD
        q = core.build_query(env, {'level': 'ERROR,WARN'}, TZ)
        self.assertEqual(q['bool']['filter'][1]['bool']['should'],
                         [{'match_phrase': {'message': 'ERROR'}}, {'match_phrase': {'message': 'WARN'}}])

    # ---- 4. elk_trace 跨索引追踪
    def test_follow_ids_prefers_configured_keys(self):
        hits = software_docs()
        self.assertEqual(core.follow_ids(make_env(), hits), [('detectId', DETECT)])
        self.assertEqual(core.follow_ids(make_env(follow_keys=['deviceId']), hits), [('deviceId', 'FRC100124020600019')])

    def test_trace_follows_business_id_into_related_index(self):
        out, _ = self.run_with(software_docs() + algo_docs(), core.op_trace, make_env(),
                               {'trace': 't-1', 'since': '7d', 'size': 500}, TZ)
        self.assertIn('按业务 ID 跨索引追踪：detectId=%s' % DETECT, out)
        self.assertIn('algo(beta-algo-*) 共 53 条，以下为开头 3 条、ERROR/WARN 1 条、最后 10 条', out)
        self.assertIn('未找到默认机型的浓度配置', out)
        self.assertTrue(out.rstrip().endswith('pipeline over'))

    def test_trace_without_business_id_or_related_off(self):
        docs = [d for d in software_docs() if 'detectId' not in d['_source']['message']] + algo_docs(1)
        out, _ = self.run_with(docs, core.op_trace, make_env(), {'trace': 't-1', 'since': '7d'}, TZ)
        self.assertIn('没有找到业务 ID', out)
        out, _ = self.run_with(software_docs() + algo_docs(1), core.op_trace, make_env(),
                               {'trace': 't-1', 'since': '7d', 'related': 'off'}, TZ)
        self.assertNotIn('关联索引', out)

    def test_mcp_trace_uses_follow(self):
        fake = FakeEs(software_docs() + algo_docs(1))
        with patch.object(m, 'resolve_env', return_value=make_env()), patch.object(core, 'call', side_effect=fake):
            out = m.handle_tool('elk_trace', {'trace_id': 't-1'})
        self.assertIn('跨索引追踪', out)

    def test_related_failure_does_not_break_main_result(self):
        fake = FakeEs(software_docs())

        def flaky(env, method, path, body=None, interactive=True):
            if 'beta-algo' in path:
                raise core.ElkError('HTTP 500 boom')
            return fake(env, method, path, body, interactive)
        docs = software_docs() + algo_docs(1)
        fake.docs = docs
        with patch.object(core, 'call', side_effect=flaky):
            out = core.op_search(make_env(), {'query': '"%s"' % DETECT, 'since': '1d'}, TZ)
        self.assertIn('total=2', out)
        self.assertIn('关联索引检查失败', out)


class RelatedConfig(unittest.TestCase):
    def load(self, **env):
        base = {'ELK_ENVS': 'test', 'ELK_TEST_URL': 'http://es.invalid'}
        base.update(env)
        return core.get_env(core.apply_tiers(core.config_from_environ(base)), 'test')

    def test_defaults_unchanged(self):
        env = self.load()
        self.assertNotIn('related', env)
        self.assertNotIn('index_fields', env)

    def test_parse_related_index_fields_follow_keys(self):
        env = self.load(ELK_TEST_RELATED='{"algo":"beta-algo-*"}', ELK_INDEX_FIELDS='{"beta-algo-*":{"level":"@message"}}',
                        ELK_TEST_FOLLOW_KEYS='detectId, orderNo')
        self.assertEqual(env['related'], {'algo': 'beta-algo-*'})
        self.assertEqual(env['index_fields'], {'beta-algo-*': {'level': '@message'}})
        self.assertEqual(env['follow_keys'], ['detectId', 'orderNo'])
        self.assertEqual(self.load(ELK_RELATED='off')['related'], 'off')

    def test_array_config_object_value(self):
        cfg = core._config_from_object({'environments': [
            {'env': 'test', 'url': 'http://es.invalid', 'related': {'algo': 'beta-algo-*'}, 'follow_keys': ['detectId']}]},
            'test-config')
        env = core.get_env(cfg, 'test')
        self.assertEqual(env['related'], {'algo': 'beta-algo-*'})
        self.assertEqual(env['follow_keys'], ['detectId'])
        self.assertIn('related algo -> beta-algo-*', core.describe_envs(cfg))

    def test_invalid_related_rejected(self):
        with self.assertRaises(core.ElkError):
            self.load(ELK_TEST_RELATED='[1,2]')


if __name__ == '__main__':
    unittest.main()
