"""Control-loop integration tests; explicitly synthetic actors, not live-model proof."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master_agent_runtime import ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.controller import Controller
from master_agent_runtime.contracts import ContractError


class FakeActor:
    threads = {}
    opens = []
    executions = {}
    workspaces = []
    module_started = None

    def __init__(self, actor_id, workspace, context, control, *, kind, **kwargs):
        self.actor_id = actor_id
        self.workspace = workspace
        self.context = context
        self.kind = kind
        self.workspaces.append((actor_id,workspace))
        self.thread_id = None
        self.session_id = None
        self.closed = False

    async def open(self, *, existing_thread=None, instructions=''):
        self.thread_id = existing_thread or 'independent-' + self.actor_id
        self.session_id = self.thread_id
        self.threads[self.actor_id] = self.thread_id
        self.opens.append((self.actor_id, existing_thread))
        return {'thread':{'id':self.thread_id,'sessionId':self.session_id},'synthetic_actor':True}

    async def start_turn(self, message, **kwargs):
        self.message = message
        self.executions[self.actor_id] = self.executions.get(self.actor_id, 0) + 1
        return 'turn-' + str(self.executions[self.actor_id])

    async def await_turn(self, turn_id, *, on_usage=None):
        if on_usage:on_usage(100*self.executions[self.actor_id])
        if self.kind == 'module':
            key = self.actor_id.rsplit(':',1)[-1]
            self.workspace.mkdir(parents=True,exist_ok=True)
            # This fixture repairs only after receiving the actual Master's
            # correction, so a missing/misdirected inbox cannot pass the loop.
            value = 0 if key == 'a' and 'Replace wrong value with 42' not in self.message else 42
            (self.workspace/(key+'.py')).write_text('value = '+str(value)+'\n')
            await asyncio.sleep(.01)
            output = {'status':'submitted','summary':'Implemented actual fixture file','issues':[]}
        else:
            failed = '"status": "failed"' in self.message
            output = {'decision':'request_changes' if failed else 'approve',
                      'rationale':'Replace wrong value with 42' if failed else 'Actual candidate meets fixture goal'}
            if 'complete integrated project' in self.message:
                output['affected_modules'] = ['a'] if failed else []
        return {'text':json.dumps(output),'turn':{'id':turn_id,'status':'completed'}}

    async def close(self):
        self.closed = True
        return {'process_exited':True,'synthetic_actor':True}


def actual_fixture_validator(steps, candidate, context, evidence, **kwargs):
    # Independently reads actual immutable files; no producer-supplied PASS.
    files = list(candidate.rglob('*.py'))
    passed = bool(files) and all('value = 42' in p.read_text() for p in files)
    return [{'status':'passed' if passed else 'failed','oracle':'frozen synthetic fixture requirement','exit_code':0 if passed else 1}]


class ControllerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = self.root/'project';project.mkdir()
        (project/'a.py').write_text('value = -1\n')
        (project/'b.py').write_text('value = -1\n')
        self.store = Store(self.root/'state'/'runtime.sqlite')
        validation = (ValidationStep(('python','frozen-oracle.py')),)
        self.spec = ProjectSpec(str(project),'Implement both coherent fixture modules',(
            ModuleSpec('a','Provide value 42',('a.py',),token_reservation=200,validation=validation),
            ModuleSpec('b','Provide value 42',('b.py',),token_reservation=200,validation=validation)),
            10000,integration_validation=validation)
        self.project = self.store.create_project(self.spec)
        FakeActor.threads={};FakeActor.opens=[];FakeActor.executions={};FakeActor.workspaces=[]

    async def test_review_rework_and_integration_without_user_relay(self):
        controller = Controller(self.store,self.project,actor_factory=FakeActor)
        with patch('master_agent_runtime.controller.validate',actual_fixture_validator):
            result = await controller.run()
        self.assertEqual(result['state'],'accepted')
        self.assertFalse(result['original_project_mutated'])
        self.assertEqual(self.store.project(self.project)['state'],'accepted')
        self.assertEqual((Path(self.spec.root)/'a.py').read_text(),'value = -1\n')
        a = self.project+':module:a'
        self.assertEqual(FakeActor.executions[a],2)
        self.assertEqual([existing for owner,existing in FakeActor.opens if owner==a],
                         [None,'independent-'+a])
        spaces = [space for owner,space in FakeActor.workspaces if owner==a]
        self.assertNotEqual(spaces[0],spaces[1])
        (spaces[0]/'a.py').write_text('late_writer = True\n')
        self.assertEqual((spaces[1]/'a.py').read_text(),'value = 42\n')
        self.assertTrue(all(m['state']=='accepted' for m in self.store.modules(self.project)))
        self.assertEqual(self.store.status(self.project)['reserved_tokens'],0)
        integrated = Path(result['path'])
        self.assertEqual((integrated/'a.py').read_text(),'value = 42\n')
        self.assertEqual((integrated/'b.py').read_text(),'value = 42\n')

    async def test_reopening_store_preserves_completed_native_context_directory(self):
        controller = Controller(self.store,self.project,actor_factory=FakeActor)
        with patch('master_agent_runtime.controller.validate',actual_fixture_validator):
            await controller.run()
        reopened = Store(self.store.path)
        self.assertEqual(reopened.actor(self.project+':module:a')['thread_id'],
                         'independent-'+self.project+':module:a')
        self.assertEqual(reopened.status(self.project)['reserved_tokens'],0)
        count=dict(FakeActor.executions)
        resumed=await Controller(reopened,self.project,actor_factory=FakeActor).run()
        self.assertEqual(resumed['state'],'accepted')
        self.assertEqual(FakeActor.executions,count)

    async def test_failed_integration_returns_to_same_module_owner(self):
        controller = Controller(self.store,self.project,actor_factory=FakeActor)
        integration_runs = []
        def validator(steps, candidate, context, evidence, **kwargs):
            if candidate.name.startswith('integration-'):
                integration_runs.append(str(candidate))
                if len(integration_runs) == 1:
                    return [{'status':'failed','oracle':'deliberate cross-module interface mismatch'}]
            return actual_fixture_validator(steps,candidate,context,evidence,**kwargs)
        with patch('master_agent_runtime.controller.validate',validator):
            result = await controller.run()
        self.assertEqual(result['state'],'accepted')
        self.assertEqual(len(integration_runs),2)
        owner = self.project+':module:a'
        self.assertEqual(FakeActor.executions[owner],3)
        self.assertEqual([existing for actor,existing in FakeActor.opens if actor==owner],
                         [None,'independent-'+owner,'independent-'+owner])
        self.assertEqual(next(m for m in self.store.modules(self.project) if m['key']=='a')['revision'],2)

    async def test_projects_do_not_share_source_context_or_module_workspace(self):
        other = self.store.create_project(self.spec)
        first = Controller(self.store,self.project,actor_factory=FakeActor)
        second = Controller(self.store,other,actor_factory=FakeActor)
        first.prepare();second.prepare()
        self.assertNotEqual(first.context,second.context)
        self.assertNotEqual(first.workspaces,second.workspaces)
        (first.context/'a.py').write_text('isolated = True\n')
        self.assertEqual((second.context/'a.py').read_text(),'value = -1\n')

    async def test_integration_applies_module_deletion_instead_of_restoring_baseline(self):
        class DeletingActor(FakeActor):
            async def await_turn(self,turn_id,*,on_usage=None):
                result = await super().await_turn(turn_id,on_usage=on_usage)
                if self.kind=='module' and self.actor_id.endswith(':a'):
                    (self.workspace/'a.py').unlink()
                return result
        def validator(steps,candidate,context,evidence,**kwargs):
            # Deleting a.py is the frozen requirement in this fixture only.
            return [{'status':'passed','oracle':'deletion fixture; controller integration is checked below'}]
        controller = Controller(self.store,self.project,actor_factory=DeletingActor)
        with patch('master_agent_runtime.controller.validate',validator):
            result = await controller.run()
        self.assertEqual(result['state'],'accepted')
        self.assertFalse((Path(result['path'])/'a.py').exists())
        self.assertTrue((Path(self.spec.root)/'a.py').exists())

    async def test_missing_integration_oracle_rejected_before_any_native_call(self):
        spec = ProjectSpec(str(self.spec.root),self.spec.objective,self.spec.modules,10000)
        project = self.store.create_project(spec)
        with self.assertRaises(ContractError):
            await Controller(self.store,project,actor_factory=FakeActor).run()
        self.assertEqual(FakeActor.opens,[])

    async def test_downstream_validation_reads_accepted_dependency_code(self):
        spec = ProjectSpec(str(self.spec.root),self.spec.objective,(
            self.spec.modules[0],ModuleSpec('b','Use accepted a',('b.py',),('a',),200,self.spec.modules[1].validation)),
            10000,integration_validation=self.spec.integration_validation)
        project = self.store.create_project(spec)
        seen = []
        def validator(steps,candidate,context,evidence,**kwargs):
            if (candidate/'b.py').exists() and not (candidate/'a.py').exists():
                seen.append((context/'a.py').read_text())
                self.assertEqual((context/'a.py').read_text(),'value = 42\n')
            return actual_fixture_validator(steps,candidate,context,evidence,**kwargs)
        with patch('master_agent_runtime.controller.validate',validator):
            result = await Controller(self.store,project,actor_factory=FakeActor).run()
        self.assertEqual(result['state'],'accepted')
        self.assertEqual(seen,['value = 42\n'])

    async def test_failed_native_scope_preflight_prevents_all_model_dispatch(self):
        from master_agent_runtime.native import NativeActor
        import sys
        with patch('master_agent_runtime.controller.check_scope',side_effect=ContractError('backend broadens writes')), \
             patch.object(NativeActor,'open',new_callable=AsyncMock) as start:
            with self.assertRaises(ContractError):
                await Controller(self.store,self.project,cli=sys.executable).run()
        start.assert_not_awaited()
        status = self.store.status(self.project)
        self.assertEqual(status['reserved_tokens'],0)
        self.assertTrue(all(a['thread_id'] is None for a in status['actors']))
        self.assertEqual([op['status'] for op in status['operations']],['failed'])
