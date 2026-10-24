import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import mcp_server as m
import elk_core as core

class DslGuard(unittest.TestCase):
    def invoke(self, body, production=True, **args):
        env={'production':production,'_name':'test','max_size':100,'timeout':60,
             '_fields':{'timestamp':'@timestamp'},'index':'logs'}
        # Request exit replaced: no credentials or network; return the actual prepared body.
        with patch.object(m,'resolve_env',return_value=env), patch.object(core,'op_raw',side_effect=lambda e,method,path,body,**kw:body):
            return m.handle_tool('elk_query_dsl',dict(body=body,**args))

    def test_production_query_always_has_time_filter(self):
        body=self.invoke({'query':{'match_all':{}}})
        self.assertIn('filter',body['query'].get('bool',{}))

    def test_long_range_rejected(self):
        with self.assertRaises(core.ElkError): self.invoke({},since='30d')

    def test_expensive_query_requires_override(self):
        with self.assertRaises(core.ElkError): self.invoke({'query':{'wildcard':{'message':'*error*'}}})

    def test_override_allows_expensive_query(self):
        body=self.invoke({'query':{'wildcard':{'message':'*error*'}}},allow_slow=True)
        self.assertIn('wildcard',body['query'])

    def test_timeout_is_capped(self):
        self.assertEqual(self.invoke({'timeout':'1h'})['timeout'],'25s')

    def test_nonproduction_preserves_query(self):
        self.assertEqual(self.invoke({'query':{'match_all':{}}},production=False)['query'],{'match_all':{}})

    def test_count_also_guarded(self):
        with self.assertRaises(core.ElkError): self.invoke({},api='_count',since='30d')

    def test_global_aggregation_requires_override(self):
        with self.assertRaises(core.ElkError): self.invoke({'aggs':{'all':{'global':{}}}})

    def test_invalid_and_reversed_time_rejected(self):
        for args in ({'since':'invalid'}, {'since':'1m','until':'2m'}, {'since':'now-30d/d'}):
            with self.subTest(args=args), self.assertRaises(core.ElkError): self.invoke({},**args)

    def test_size_and_short_timeout(self):
        body=self.invoke({'size':999,'timeout':'500ms'})
        self.assertEqual(body['size'],100)
        self.assertEqual(body['timeout'],'500ms')

    def test_nested_expensive_query_rejected(self):
        with self.assertRaises(core.ElkError):
            self.invoke({'query':{'bool':{'must':[{'query_string':{'query':'*error'}}]}}})

    def test_input_body_is_unchanged(self):
        body={'query':{'term':{'service':'api'}}}
        result=self.invoke(body,since='1h')
        self.assertEqual(body,{'query':{'term':{'service':'api'}}})
        self.assertEqual(result['query']['bool']['must'],[body['query']])

    def test_runtime_api_validation(self):
        with self.assertRaises(core.ElkError): self.invoke({},api='_delete_by_query')

if __name__=='__main__': unittest.main()
