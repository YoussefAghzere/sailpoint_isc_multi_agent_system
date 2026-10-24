"""Unified ISC resource-version paths, independent of legacy year bundles."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from isc_agents.platform.catalog import Catalog, OFFICIAL_ROOT, API_DOCUMENTATION
from isc_agents.platform.db import DB
from isc_agents.platform.environments import Environments
from isc_agents.platform.security import Vault
from isc_agents.platform.isc import ISC
from isc_agents.platform.network import HTTP
from isc_agents.platform.jobs import Worker
from test_studio import operation, ID, SOURCE
from isc_agents.models import Rejected
from isc_agents.platform.workflows import Workflows


UNIFIED = {'openapi':'3.0.1','info':{'title':'Identity Security Cloud API','version':'v1'},'paths':{
    '/widgets/v1':{'get':operation('listWidgetsV1',response={'type':'array','items':{'type':'object'}})},
    '/widgets/v2/{id}':{'get':operation('getWidgetV2',parameters=[ID],response={'type':'object'})},
    '/.well-known/example':{'get':operation('getDiscoveryV1',response={'type':'object'})}}}


class OfficialCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=DB(Path(self.tmp.name)/'catalog.db')
        self.catalog=Catalog(self.db)
    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_sync_imports_unified_bundle_without_guessing_years(self):
        urls=[]
        def transport(request):
            urls.append(str(request.url));return httpx.Response(200,json=UNIFIED)
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result=self.catalog.sync_latest(HTTP(client))
        self.assertEqual(urls,[OFFICIAL_ROOT+'dereferenced/deref-sailpoint-api.json'])
        self.assertEqual(result['version'],'isc')
        self.assertEqual(result['documentation_url'],API_DOCUMENTATION)
        self.assertEqual(result['operations'],3)
        self.assertEqual(self.catalog.prepare(result['id'],'getWidgetV2',{'path':{'id':'abc'}})['resolved_path'],'/widgets/v2/abc')
        self.assertEqual(self.catalog.prepare(result['id'],'getDiscoveryV1',{})['resolved_path'],'/.well-known/example')

    def test_live_client_uses_exact_resource_version_path(self):
        spec=self.catalog.install(UNIFIED,'isc',OFFICIAL_ROOT+'dereferenced/deref-sailpoint-api.json')
        envs=Environments(self.db,Vault(self.tmp.name))
        env=envs.save({'name':'Synthetic','client':'test','stage':'sandbox','backend':'isc',
            'base_url':'https://test.api.identitynow.com','client_id':'synthetic-client','client_secret':'synthetic-secret','spec_id':spec['id']},'admin')
        paths=[]
        def transport(request):
            paths.append(request.url.path)
            return httpx.Response(200,json={'access_token':'synthetic-token'} if request.url.path=='/oauth/token' else {'id':'abc'})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result=ISC(self.db,envs,HTTP(client),env['id']).call('getWidgetV2',{'path':{'id':'abc'}})
        self.assertEqual(paths,['/oauth/token','/widgets/v2/abc'])
        self.assertEqual(result['data']['id'],'abc')

    def test_unified_source_creation_keeps_duplicate_detection(self):
        spec=copy.deepcopy(UNIFIED)
        spec['paths']['/sources/v1']={
            'get':operation('listSourcesV1',parameters=[{'in':'query','name':'filters','schema':{'type':'string'}},{'in':'query','name':'limit','schema':{'type':'integer'}}],response={'type':'array','items':{'type':'object'}}),
            'post':operation('createSourceV1',body=SOURCE,status='201')}
        spec['paths']['/sources/v1/{id}']={'get':operation('getSourceV1',parameters=[ID])}
        imported=self.catalog.install(spec,'isc','https://example.test/unified.json')
        envs=Environments(self.db,Vault(self.tmp.name))
        env=envs.save({'name':'Synthetic','client':'test','stage':'sandbox','backend':'isc','write_enabled':True,
            'allowed_write_operations':['createSourceV1'],'base_url':'https://test.api.identitynow.com',
            'client_id':'synthetic-client','client_secret':'synthetic-secret','spec_id':imported['id']},'admin')
        paths=[]
        def transport(request):
            paths.append((request.method,request.url.path))
            return httpx.Response(200,json={'access_token':'synthetic-token'} if request.url.path=='/oauth/token' else [{'id':'existing','name':'HR'}])
        proposal={'title':'Create source','questions':[],'steps':[
            {'id':'create','operation_id':'createSourceV1','arguments':{'body':{'name':'HR','connector':'test','owner':{'id':'owner'}}}},
            {'id':'check','operation_id':'getSourceV1','arguments':{'path':{'id':'${steps.create.id}'}}}],
            'checks':[{'step_id':'check','pointer':'/name','equals':'HR'}]}
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            factory=lambda eid:ISC(self.db,envs,HTTP(client),eid)
            with self.assertRaisesRegex(Rejected,'already exists'):
                Workflows(self.db,envs,factory).compile(env['id'],proposal,'admin')
        self.assertEqual(paths,[('POST','/oauth/token'),('GET','/sources/v1')])

    def test_sync_does_not_repin_existing_environments_and_indexes_unified_docs(self):
        legacy=copy.deepcopy(UNIFIED);legacy['paths']={'/widgets':UNIFIED['paths']['/widgets/v1']}
        old=self.catalog.install(legacy,'v2026','https://example.test/legacy.json')
        envs=Environments(self.db,Vault(self.tmp.name))
        env=envs.save({'name':'Synthetic','client':'test','stage':'sandbox','backend':'isc',
            'base_url':'https://test.api.identitynow.com','client_id':'synthetic-client','client_secret':'synthetic-secret','spec_id':old['id']},'admin')
        self.db.enqueue('sync_specs',None,{})
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=UNIFIED))) as client:
            Worker(self.db,envs,HTTP(client)).run_one()
        job=self.db.one('SELECT * FROM jobs')
        self.assertEqual(job['state'],'completed',job['error'])
        self.assertEqual(json.loads(job['result'])['indexed_operations'],3)
        self.assertEqual(envs.get(env['id'])['spec_id'],old['id'])
        self.assertEqual(self.catalog.prepare(old['id'],'listWidgetsV1',{})['resolved_path'],'/v2026/widgets')
