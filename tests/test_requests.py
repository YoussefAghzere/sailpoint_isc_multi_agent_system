"""Agent chooses actions: no business-specific dispatch before model decisions."""
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from isc_agents.models import Rejected
from isc_agents.platform.server import Context, Settings, create_app
from isc_agents.platform.jobs import Worker
from isc_agents.platform.network import HTTP
from isc_agents.platform.python_runtime import PythonSandbox
from test_studio import SPEC, ID, operation


def identity_spec():
    # One fixture domain alongside sources. Runtime has no identity-specific code.
    spec = copy.deepcopy(SPEC)
    spec['paths']['/identities'] = {'get': operation('listIdentities', parameters=[
        {'in':'query','name':'filters','schema':{'type':'string'}},
        {'in':'query','name':'limit','schema':{'type':'integer','maximum':250}}],
        response={'type':'array','items':{'type':'object'}})}
    return spec


class ScriptedModel:
    def __init__(self, actions):
        self.actions, self.calls = list(actions), []
    def generate(self, role, context, schema=None):
        self.calls.append((role, context))
        action = self.actions.pop(0)
        if callable(action):
            action = action(context)
        kind, args = action
        return {'action': kind, 'summary': 'Synthetic agent action', 'arguments':json.dumps(args)}


def cite_last(context):
    return ('respond', {'answer':'Observed result.', 'citations':[context['history'][-1]['observation_id']]})


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(directory=self.tmp.name,worker=False,auto_ingestion=False,
            bootstrap_password='synthetic-admin-password',allowed_hosts=('testserver',))
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        login = self.client.post('/api/login',json={'username':'admin','password':'synthetic-admin-password'}).json()
        self.headers = {'X-CSRF-Token':login['csrf']}
        self.ctx = Context(self.settings)
        self.spec = self.ctx.catalog.install(identity_spec(),'v2026','https://example.test/contract.json')
        self.env = self.ctx.envs.save({'name':'Test','client':'test','backend':'isc','stage':'sandbox',
            'base_url':'https://test.api.identitynow.com','client_id':'synthetic-id','client_secret':'synthetic-secret',
            'spec_id':self.spec['id']},'admin')
        self.profile = self.ctx.models.save('admin',{'name':'Qwen','provider':'local','model':'qwen'})
        self.ctx.models.select('admin',self.profile['id'])
        self.requests = []
        def transport(request):
            self.requests.append(request)
            if request.url.path == '/oauth/token':
                return httpx.Response(200,json={'access_token':'synthetic-token','expires_in':300})
            if request.url.path == '/v2026/identities':
                return httpx.Response(200,json=[{'id':'identity-1','name':'Jane Doe','emailAddress':'jane@example.com'}])
            if request.url.path == '/v2026/sources':
                return httpx.Response(200,json=[])
            return httpx.Response(404)
        self.http = HTTP(httpx.Client(transport=httpx.MockTransport(transport)))

    def tearDown(self):
        self.http.close(); self.ctx.close(); self.client.close(); self.tmp.cleanup()

    def queue(self, prompt='Retrieve information about Jane Doe', **extra):
        response=self.client.post(f"/api/environments/{self.env['id']}/plan", headers=self.headers,json={'prompt':prompt,**extra})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['task'],'plan')
        return response.json()['job_id']

    def work(self, model):
        self.assertTrue(Worker(self.ctx.db,self.ctx.envs,self.http,model=model).run_one())
        return self.ctx.db.one('SELECT * FROM jobs ORDER BY created DESC LIMIT 1')

    def answer(self,row):
        self.assertEqual(row['state'],'completed',row['error'])
        return self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])

    def test_no_prompt_or_example_selects_api_without_model(self):
        for prompt in ('Retrieve information about Jane Doe','Explain correlation','Analyze our sources'):
            self.queue(prompt,task='identity_lookup',identity_query='ignored legacy field')
        self.assertEqual(self.requests,[])
        self.assertEqual([r['kind'] for r in self.ctx.db.all('SELECT kind FROM jobs')],['plan']*3)
        self.ctx.models.select('admin','')
        with patch.dict(os.environ,{},clear=True):
            response=self.client.post(f"/api/environments/{self.env['id']}/plan",headers=self.headers,json={'prompt':'Find identity Jane Doe'})
        self.assertEqual(response.status_code,400)

    def test_agent_decides_to_ask_without_any_tenant_api_call(self):
        self.queue()
        model=ScriptedModel([('ask',{'questions':['Which tenant record should I investigate?']})])
        answer=self.answer(self.work(model))
        self.assertEqual(answer['status'],'needs_input')
        self.assertEqual(answer['model_calls'],1)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.ctx.db.all('SELECT * FROM live_runs'),[])

    def test_agent_discovers_operation_then_reads_and_answers(self):
        self.queue()
        model=ScriptedModel([('catalog',{'query':'identities'}),
            ('catalog',{'operation_id':'listIdentities'}),
            ('read_api',{'operation_id':'listIdentities','arguments':{'query':{'filters':'name eq "Jane Doe"','limit':10}}}),
            lambda context: ('respond',{'answer':'Jane Doe: jane@example.com','citations':[context['history'][-1]['observation_id']]})])
        answer=self.answer(self.work(model))
        self.assertIn('jane@example.com',answer['answer'])
        self.assertEqual(answer['model_calls'],4)
        self.assertEqual([e['action'] for e in answer['trace']],['catalog','catalog','read_api','respond'])
        self.assertEqual([r.method for r in self.requests],['POST','GET'])
        self.assertNotIn('jane@example.com',self.ctx.db.one('SELECT content FROM answers')['content'])
        self.assertNotIn('synthetic-secret',json.dumps(model.calls))

    def test_agent_can_choose_a_different_resource_with_same_runtime(self):
        self.queue('Inspect configured source systems')
        model=ScriptedModel([('read_api',{'operation_id':'listSources','arguments':{'query':{'limit':5}}}),cite_last])
        answer=self.answer(self.work(model))
        self.assertEqual(answer['model_calls'],2)
        self.assertTrue(self.requests[-1].url.path.endswith('/sources'))

    def test_agent_creates_and_uses_read_tool_during_request(self):
        self.queue('Create a tool for reading source inventory and use it')
        def invoke(context):
            created=json.loads(context['history'][-1]['result'])
            return ('invoke_tool',{'tool_id':created['id'],'arguments':{'query':{'limit':5}}})
        model=ScriptedModel([('create_tool',{'name':'Read inventory','description':'Source inventory','operation_id':'listSources'}),invoke,cite_last])
        self.answer(self.work(model))
        self.assertEqual(self.ctx.db.one('SELECT status FROM tools')['status'],'active')
        self.assertTrue(self.requests[-1].url.path.endswith('/sources'))

    def test_write_tool_is_pending_and_cannot_mutate_from_agent_loop(self):
        self.queue('Create a source-writing tool')
        model=ScriptedModel([('create_tool',{'name':'Create source','description':'Write source configuration','operation_id':'createSource'}),cite_last])
        self.answer(self.work(model))
        self.assertEqual(self.ctx.db.one('SELECT status FROM tools')['status'],'proposed')
        self.assertEqual(self.requests,[])

    def test_direct_write_tool_call_is_rejected(self):
        self.queue('Try a write before approval')
        model=ScriptedModel([('read_api',{'operation_id':'createSource','arguments':{'body':{'name':'HR','connector':'test','owner':{'id':'x'}}}}),cite_last])
        answer=self.answer(self.work(model))
        self.assertEqual(answer['trace'][0]['status'],'tool_rejected')
        self.assertEqual(self.requests,[])

    def test_invented_citations_are_rejected(self):
        self.queue()
        row=self.work(ScriptedModel([('respond',{'answer':'Unsupported','citations':['imaginary']})]))
        self.assertEqual(row['state'],'failed')
        self.assertIn('not observed',row['error'])
        self.assertEqual(self.ctx.db.all('SELECT * FROM answers'),[])

    def test_answer_cannot_be_retrieved_through_other_environment(self):
        self.queue()
        answer=self.answer(self.work(ScriptedModel([('respond',{'answer':'General guidance','citations':[]})])))
        other=self.ctx.envs.save({'name':'Other','client':'other','stage':'sandbox','backend':'simulator'},'admin')
        self.assertEqual(self.client.get(f"/api/environments/{other['id']}/answers/{answer['id']}").status_code,400)

    def test_changed_environment_rejects_queued_agent_request(self):
        self.queue()
        self.ctx.db.execute("UPDATE environments SET revision='changed' WHERE id=?",(self.env['id'],))
        row=self.work(ScriptedModel([]))
        self.assertEqual(row['state'],'failed')
        self.assertEqual(self.requests,[])

    def test_python_action_records_execution_and_forwards_only_read_capabilities(self):
        self.queue('Compute source count using Python')
        def sandbox(code,inputs,bridge):
            result=bridge('read_api',{'operation_id':'listSources','arguments':{'query':{'limit':10}}})
            self.assertIn('observation_id',result)
            with self.assertRaises(Rejected):
                bridge('run_python',{})
            return {'status':'completed','result':len(result['result']['data']),'stdout':'','stderr':''}
        model=ScriptedModel([('run_python',{'name':'count.py','code':'result=0','inputs':{}}),cite_last])
        with patch.object(PythonSandbox,'run',side_effect=sandbox):
            answer=self.answer(self.work(model))
        self.assertEqual(answer['budget']['tools'],2)
        review=json.loads(self.ctx.db.one('SELECT review FROM drafts')['review'])
        self.assertTrue(review['executed'])
        result=self.client.get(f"/api/environments/{self.env['id']}/code-runs/{review['execution_id']}")
        self.assertEqual(result.json()['result'],0)
        self.assertNotIn('"result":0',self.ctx.db.one('SELECT content FROM code_runs')['content'])

    @unittest.skipUnless(os.environ.get('ISC_TEST_DOCKER')=='1','requires real Docker runtime image')
    def test_agent_generated_code_runs_through_real_container_and_gateway(self):
        self.queue('Use Python to inspect source inventory and compute a sum')
        code='records=tools.call("read_api", {"operation_id":"listSources","arguments":{"query":{"limit":10}}})["result"]["data"]; result={"source_count":len(records),"computed":sum(inputs["values"])}'
        model=ScriptedModel([('run_python',{'name':'inspect.py','code':code,'inputs':{'values':[2,5]}}),cite_last])
        answer=self.answer(self.work(model))
        review=json.loads(self.ctx.db.one('SELECT review FROM drafts')['review'])
        execution=self.ctx.vault.decrypt(self.ctx.db.one('SELECT content FROM code_runs')['content'])
        self.assertTrue(review['executed'])
        self.assertEqual(execution['status'],'completed',execution)
        self.assertEqual(execution['result'],{'source_count':0,'computed':7})
        self.assertEqual([r.method for r in self.requests],['POST','GET'])
        self.assertEqual(answer['budget']['tools'],2)
        self.assertTrue(any(o.get('execution_id') for o in answer['evidence']))

    def test_python_runtime_unavailable_is_reported_without_host_fallback(self):
        self.queue('Run a Python calculation')
        with patch.object(PythonSandbox,'run',side_effect=Rejected('Docker is unavailable')):
            answer=self.answer(self.work(ScriptedModel([('run_python',{'name':'calc.py','code':'result=2+2'}),cite_last])))
        review=json.loads(self.ctx.db.one('SELECT review FROM drafts')['review'])
        self.assertFalse(review['executed'])
        self.assertEqual(review['status'],'rejected')
        self.assertEqual(self.requests,[])

    def test_catalog_cache_reuses_parse_and_refreshes_metadata(self):
        first=self.ctx.catalog.get(self.spec['id'])
        with patch('isc_agents.platform.catalog.json.loads',side_effect=AssertionError('reparsed')):
            self.assertIs(first['document'],self.ctx.catalog.get(self.spec['id'])['document'])
        self.ctx.db.execute('UPDATE specs SET fetched=123 WHERE id=?',(self.spec['id'],))
        self.assertEqual(self.ctx.catalog.get(self.spec['id'])['fetched'],123)
