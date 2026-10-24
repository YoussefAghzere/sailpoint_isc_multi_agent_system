"""Runtime policy and opt-in tests against real disposable Docker containers."""
import os
import unittest
from unittest.mock import patch

from isc_agents.models import Rejected
from isc_agents.platform.python_runtime import PythonSandbox


class RuntimePolicyTests(unittest.TestCase):
    def test_command_enforces_isolation_without_host_mounts(self):
        with patch.dict(os.environ,{},clear=True),patch('shutil.which',return_value='/usr/bin/docker'):
            command=PythonSandbox().command('isc-test')
        for required in ('--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=32','--memory=128m','--pull=never'):
            self.assertIn(required,command)
        self.assertFalse(any(c.startswith(('--mount','--volume','--privileged','--network=host')) for c in command))
        self.assertIn('--user=65534:65534',command)

    def test_missing_disabled_or_remote_runtime_never_falls_back_to_host_python(self):
        with patch.dict(os.environ,{},clear=True),patch('shutil.which',return_value=None):
            with self.assertRaisesRegex(Rejected,'Docker'):
                PythonSandbox().command('test')
        with patch.dict(os.environ,{'ISC_ENABLE_PYTHON':'0'}):
            with self.assertRaisesRegex(Rejected,'disabled'):
                PythonSandbox().command('test')
        with patch.dict(os.environ,{'ISC_PYTHON_DOCKER_HOST':'tcp://example.test:2375'}):
            with self.assertRaisesRegex(Rejected,'local'):
                PythonSandbox().command('test')

    def test_image_cannot_be_an_extra_docker_flag(self):
        with patch.dict(os.environ,{'ISC_PYTHON_IMAGE':'--privileged'}):
            with self.assertRaisesRegex(Rejected,'image reference'):
                PythonSandbox().command('test')


@unittest.skipUnless(os.environ.get('ISC_TEST_DOCKER')=='1','set ISC_TEST_DOCKER=1 after building sandbox image')
class RealDockerTests(unittest.TestCase):
    def test_generated_python_computes_and_returns_json(self):
        result=PythonSandbox().run('print("computed"); result=sum(inputs["values"])',{'values':[2,3,5]},lambda *_:None)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['result'],10)
        self.assertEqual(result['stdout'],'computed')

    def test_sdk_round_trip_uses_host_controlled_read_tools(self):
        calls=[]
        def bridge(name,args):
            calls.append((name,args))
            return {'observation_id':'obs_1','result':{'data':[{'count':2},{'count':3}]}}
        code='records=tools.call("read_api", {"operation_id":"chosenOperation","arguments":{}}); result=sum(r["count"] for r in records["result"]["data"])'
        result=PythonSandbox().run(code,{},bridge)
        self.assertEqual(result['status'],'completed',result['stderr'])
        self.assertEqual(result['result'],5)
        self.assertEqual(calls[0][0],'read_api')
        self.assertEqual(result['tool_calls'],1)

    def test_sdk_rejects_unauthorized_capabilities_before_dispatch(self):
        calls=[]
        code='try:\n tools.call("run_python", {})\nexcept RuntimeError as exc:\n result=str(exc)'
        result=PythonSandbox().run(code,{},lambda *args:calls.append(args))
        self.assertEqual(result['status'],'completed')
        self.assertIn('only documented read capabilities',result['result'])
        self.assertEqual(calls,[])

    def test_no_host_files_secrets_socket_or_external_network(self):
        code='''import os, socket
result={"uid":os.getuid(),"workspace":os.path.exists("/workspace"),"socket":os.path.exists("/var/run/docker.sock"),"key_present":bool(os.environ.get("OPENAI_API_KEY")),"proxy_present":bool(os.environ.get("HTTPS_PROXY"))}
try:
    socket.create_connection(("203.0.113.1",443),timeout=.2).close()
    result["network"]=True
except OSError:
    result["network"]=False
'''
        with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-host-secret'}):
            result=PythonSandbox().run(code,{},lambda *_:None)['result']
        self.assertEqual(result,{'uid':65534,'workspace':False,'socket':False,'key_present':False,'proxy_present':False,'network':False})

    def test_infinite_loop_is_stopped(self):
        with self.assertRaisesRegex(Rejected,'wall-time'):
            PythonSandbox(timeout=2).run('while True: pass',{},lambda *_:None)

    def test_excessive_output_is_stopped(self):
        with self.assertRaisesRegex(Rejected,'limit'):
            PythonSandbox().run('print("x"*300000)',{},lambda *_:None)

    def test_tool_call_budget_is_enforced(self):
        calls=[]
        with self.assertRaisesRegex(Rejected,'tool-call budget'):
            PythonSandbox(max_tools=2).run('for i in range(3): tools.call("catalog", {"query":"sources"})',{},lambda *args:calls.append(args))
        self.assertEqual(len(calls),2)

    def test_code_exception_is_a_failed_execution_not_success(self):
        result=PythonSandbox().run('raise ValueError("generated code error")',{},lambda *_:None)
        self.assertEqual(result['status'],'failed')
        self.assertIn('ValueError',result['stderr'])
