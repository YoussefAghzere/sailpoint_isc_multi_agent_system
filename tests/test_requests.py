"""Task outcomes against synthetic ISC contracts; no LLM or tenant credentials required."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from isc_agents.models import Rejected
from isc_agents.platform.requests import Answers, IdentityLookup, route
from isc_agents.platform.server import Context, Settings, create_app
from isc_agents.platform.jobs import Worker
from isc_agents.platform.network import HTTP
from test_studio import SPEC, ID, operation


def identity_spec():
    spec = copy.deepcopy(SPEC)
    spec['paths']['/identities'] = {'get': operation('listIdentities', parameters=[
        {'in':'query','name':'filters','schema':{'type':'string'}},
        {'in':'query','name':'limit','schema':{'type':'integer','maximum':250}},
        {'in':'query','name':'defaultFilter','schema':{'enum':['NONE','CORRELATED_ONLY']}}],
        response={'type':'array','items':{'type':'object'}})}
    spec['paths']['/identities/{id}'] = {'get': operation('getIdentity',parameters=[ID],response={'type':'object'})}
    return spec


class RoutingTests(unittest.TestCase):
    def test_identity_request_routes_without_role_mining(self):
        for prompt in ('retrieve information about Youssef Aghzere', 'Find identity Jane Doe', 'Who is Jane Doe?', 'Get details for jane@example.com'):
            with self.subTest(prompt=prompt):
                self.assertEqual(route(prompt)['task'],'identity_lookup')
        self.assertEqual(route('retrieve information about Youssef Aghzere')['subject'],'Youssef Aghzere')

    def test_changes_and_other_scopes_do_not_become_identity_lookups(self):
        for prompt in ('Find identity Jane Doe and disable it', 'Retrieve information about role mining', 'Integrate an HR source', 'Show access for Jane Doe'):
            with self.subTest(prompt=prompt):
                self.assertEqual(route(prompt)['task'],'integration')
        self.assertEqual(route('Explain account correlation')['task'],'explanation')
        self.assertEqual(route('Explain account correlation','integration')['task'],'integration')

    def test_explicit_lookup_accepts_separate_subject(self):
        self.assertEqual(route('Show a profile','identity_lookup','jane@example.com')['subject'],'jane@example.com')
        with self.assertRaises(Rejected):
            route('x','identity_lookup','bad\nvalue')
        with self.assertRaises(Rejected):
            route('x','invented_task')


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
        self.spec = self.ctx.catalog.install(identity_spec(),'v2026','https://example.test/identities.json')
        self.env = self.ctx.envs.save({'name':'Test','client':'test','backend':'isc','stage':'sandbox',
            'base_url':'https://test.api.identitynow.com','client_id':'synthetic-id','client_secret':'synthetic-secret',
            'spec_id':self.spec['id']},'admin')
        self.person = {'id':'identity-1','name':'jdoe','firstname':'Jane','lastname':'Doe','email':'jane@example.com',
            'cloudStatus':'ACTIVE','attributes':{'private':'do not display'},'password':'do not display'}
        self.matches = [self.person]
        self.requests = []
        self.bad_detail = False
        def transport(request):
            self.requests.append(request)
            if request.url.path == '/oauth/token':
                return httpx.Response(200,json={'access_token':'synthetic-token','expires_in':300})
            if request.url.path == '/v2026/identities':
                return httpx.Response(200,json=self.matches)
            if request.url.path.startswith('/v2026/identities/'):
                identity_id = request.url.path.rsplit('/',1)[-1]
                return httpx.Response(200,json={**self.person,'id':'wrong' if self.bad_detail else identity_id})
            return httpx.Response(404)
        self.http = HTTP(httpx.Client(transport=httpx.MockTransport(transport)))

    def tearDown(self):
        self.http.close()
        self.ctx.close()
        self.client.close()
        self.tmp.cleanup()

    def queue(self,prompt='Retrieve information about Jane Doe',**extra):
        with patch.dict('os.environ',{},clear=True):
            response = self.client.post(f"/api/environments/{self.env['id']}/plan",headers=self.headers,json={'prompt':prompt,**extra})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def work(self,model=None):
        worker = Worker(self.ctx.db,self.ctx.envs,self.http,model=model)
        self.assertTrue(worker.run_one())
        return self.ctx.db.one('SELECT * FROM jobs ORDER BY created DESC LIMIT 1')

    def test_lookup_returns_profile_with_no_model_or_mutation(self):
        job = self.queue()
        self.assertEqual(job['task'],'identity_lookup')
        row = self.work()
        self.assertEqual(row['state'],'completed',row['error'])
        result = json.loads(row['result'])
        answer = self.ctx.answers.get(self.env['id'],result['answer_id'])
        self.assertEqual(answer['identity']['email'],'jane@example.com')
        self.assertEqual(answer['model_calls'],0)
        self.assertEqual([e['operation_id'] for e in answer['evidence']],['listIdentities','getIdentity'])
        self.assertNotIn('attributes',answer['identity'])
        self.assertNotIn('password',answer['identity'])
        self.assertNotIn('jane@example.com',row['result'])
        encrypted = self.ctx.db.one('SELECT content FROM answers')['content']
        self.assertNotIn('jane@example.com',encrypted)
        self.assertEqual([r.method for r in self.requests],['POST','GET','GET'])
        params = self.requests[1].url.params
        self.assertIn('firstname eq "Jane" and lastname eq "Doe"',params['filters'])
        self.assertEqual(params['defaultFilter'],'NONE')
        self.assertEqual(params['limit'],'21')
        self.assertEqual(self.ctx.db.all('SELECT * FROM live_runs'),[])

    def test_ambiguous_match_requires_selection_and_validates_candidate(self):
        self.matches = [self.person,{**self.person,'id':'identity-2','email':'jane2@example.com'}]
        self.queue();row = self.work()
        aid = json.loads(row['result'])['answer_id']
        answer = self.ctx.answers.get(self.env['id'],aid)
        self.assertEqual(answer['status'],'needs_selection')
        self.assertEqual(len(self.requests),2)
        path = f"/api/environments/{self.env['id']}/answers/{aid}/select"
        self.assertEqual(self.client.post(path,headers=self.headers,json={'identity_id':'unrelated'}).status_code,400)
        self.assertEqual(self.client.post(path,headers=self.headers,json={'identity_id':'identity-2'}).status_code,200)
        row = self.work()
        chosen = self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])
        self.assertEqual(chosen['identity']['id'],'identity-2')
        self.assertEqual(chosen['evidence'][0]['operation_id'],'getIdentity')

    def test_no_match_does_not_invent_questions_or_roles(self):
        self.matches = []
        self.queue();row = self.work()
        answer = self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])
        self.assertEqual(answer['status'],'not_found')
        self.assertNotIn('questions',answer)
        self.assertEqual(len(self.requests),2)

    def test_truncated_matches_are_not_silently_selected(self):
        self.matches = [{**self.person,'id':f'id-{i}'} for i in range(21)]
        self.queue();row = self.work()
        answer = self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])
        self.assertTrue(answer['truncated'])
        self.assertEqual(len(answer['candidates']),20)

    def test_selection_and_queued_reads_are_invalidated_by_environment_edit(self):
        self.queue()
        self.ctx.db.execute("UPDATE environments SET revision='changed' WHERE id=?",(self.env['id'],))
        row = self.work()
        self.assertEqual(row['state'],'failed')
        self.assertIn('Environment changed',row['error'])
        self.assertEqual(self.requests,[])

    def test_mismatched_details_fail_closed(self):
        self.bad_detail = True
        self.queue();row = self.work()
        self.assertEqual(row['state'],'failed')
        self.assertIn('did not match',row['error'])
        self.assertEqual(self.ctx.db.all('SELECT * FROM answers'),[])

    def test_answer_access_is_environment_scoped(self):
        self.queue();row=self.work();aid=json.loads(row['result'])['answer_id']
        self.assertEqual(self.client.get(f"/api/environments/{self.env['id']}/answers/{aid}").status_code,200)
        other=self.ctx.envs.save({'name':'Other','client':'other','stage':'sandbox','backend':'simulator'},'admin')
        self.assertEqual(self.client.get(f"/api/environments/{other['id']}/answers/{aid}").status_code,400)
        self.client.post('/api/users',headers=self.headers,json={'username':'operator','password':'synthetic-user-password','role':'operator','environments':[]})
        login=self.client.post('/api/login',json={'username':'operator','password':'synthetic-user-password'}).json()
        self.assertEqual(self.client.get(f"/api/environments/{self.env['id']}/answers/{aid}").status_code,400)

    def test_documentation_answer_uses_one_call_and_validates_citations(self):
        self.ctx.models.select('admin',self.ctx.models.save('admin',{'name':'Qwen','provider':'local','model':'qwen'})['id'])
        self.ctx.research.ingest('https://documentation.sailpoint.com/saas/help/correlation','Account correlation matches accounts to identities using configured rules.','Correlation','official')
        calls=[]
        class Model:
            def generate(self,role,context,schema=None):
                calls.append(context)
                return {'answer':'Correlation matches accounts to identities.','citations':[context['evidence'][0]['id']]}
        queued=self.queue('Explain account correlation')
        self.assertEqual(queued['task'],'explanation')
        row=self.work(Model())
        self.assertEqual(row['state'],'completed',row['error'])
        answer=self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])
        self.assertEqual(answer['model_calls'],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.requests,[])

    def test_reviewer_can_lookup_but_cannot_plan_mutations(self):
        self.client.post('/api/users',headers=self.headers,json={'username':'reviewer','password':'synthetic-user-password','role':'reviewer','environments':[self.env['id']]})
        login=self.client.post('/api/login',json={'username':'reviewer','password':'synthetic-user-password'}).json()
        self.headers={'X-CSRF-Token':login['csrf']}
        self.queue()
        response=self.client.post(f"/api/environments/{self.env['id']}/plan",headers=self.headers,
            json={'prompt':'Create a source','task':'integration'})
        self.assertEqual(response.status_code,400)
        self.assertIn('Reviewers cannot',response.json()['error'])

    def test_v2026_profile_fields_are_normalized_without_custom_attributes(self):
        self.person = {'id':'identity-1','name':'Jane Doe','alias':'jdoe',
            'emailAddress':'jane@example.com','identityStatus':'ACTIVE',
            'managerRef':{'id':'manager-1','name':'Manager'},
            'attributes':{'firstname':'Jane','lastname':'Doe','privateCustomField':'excluded'}}
        self.matches = [self.person]
        self.queue();row=self.work()
        profile=self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])['identity']
        self.assertEqual(profile['firstname'],'Jane')
        self.assertEqual(profile['lastname'],'Doe')
        self.assertEqual(profile['email'],'jane@example.com')
        self.assertEqual(profile['identityStatus'],'ACTIVE')
        self.assertEqual(profile['manager']['id'],'manager-1')
        self.assertNotIn('privateCustomField',json.dumps(profile))

    def test_email_and_id_lookup_stay_in_profile_scope(self):
        for value in ('jane@example.com','a'*32):
            with self.subTest(query=value):
                self.requests.clear()
                queued=self.queue(task='identity_lookup',identity_query=value)
                row=self.work()
                self.assertEqual(row['state'],'completed',row['error'])
                if '@' in value:
                    self.assertEqual(self.requests[1].url.params['filters'],'email eq "jane@example.com"')
                else:
                    self.assertEqual(len(self.requests),2)  # OAuth and details only
                    self.assertTrue(self.requests[1].url.path.endswith('/'+'a'*32))

    def test_environment_changed_during_read_is_not_published_as_current(self):
        queued=self.queue()
        original=IdentityLookup.run
        def changing(lookup,*args,**kwargs):
            data=original(lookup,*args,**kwargs)
            self.ctx.db.execute("UPDATE environments SET revision='changed' WHERE id=?",(self.env['id'],))
            return data
        with patch.object(IdentityLookup,'run',changing):
            row=self.work()
        self.assertEqual(row['state'],'failed')
        self.assertEqual(self.ctx.db.all('SELECT * FROM answers'),[])

    def test_no_documentation_evidence_avoids_generation(self):
        self.ctx.models.select('admin',self.ctx.models.save('admin',{'name':'Qwen','provider':'local','model':'qwen'})['id'])
        class Model:
            def generate(self,*args,**kwargs):
                raise AssertionError('Do not generate unsupported answers')
        self.queue('Explain account correlation')
        row=self.work(Model())
        self.assertEqual(row['state'],'completed',row['error'])
        answer=self.ctx.answers.get(self.env['id'],json.loads(row['result'])['answer_id'])
        self.assertEqual(answer['status'],'needs_input')
        self.assertEqual(answer['model_calls'],0)

    def test_documentation_rejects_invented_citations(self):
        self.ctx.models.select('admin',self.ctx.models.save('admin',{'name':'Qwen','provider':'local','model':'qwen'})['id'])
        self.ctx.research.ingest('https://documentation.sailpoint.com/example','Correlation documentation','Correlation','official')
        class Model:
            def generate(self,*args,**kwargs):
                return {'answer':'Unsupported','citations':['invented-source']}
        self.queue('Explain account correlation')
        row=self.work(Model())
        self.assertEqual(row['state'],'failed')
        self.assertIn('unsupported evidence',row['error'])
        self.assertEqual(self.ctx.db.all('SELECT * FROM answers'),[])

    def test_catalog_cache_reuses_parse_and_refreshes_metadata(self):
        first=self.ctx.catalog.get(self.spec['id'])
        with patch('isc_agents.platform.catalog.json.loads',side_effect=AssertionError('reparsed')):
            second=self.ctx.catalog.get(self.spec['id'])
            self.assertIs(first['document'],second['document'])
        self.ctx.db.execute('UPDATE specs SET fetched=123 WHERE id=?',(self.spec['id'],))
        self.assertEqual(self.ctx.catalog.get(self.spec['id'])['fetched'],123)
