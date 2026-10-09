"""Reconcile acknowledged terminal turns; never respawn an unknown dispatch."""
from pathlib import Path
import json

from .artifacts import capture,context_hashes,file_names,owned_path,verify
from .contracts import ConflictError
from .native import JsonRpcClient
from .lease import project_lease


async def reconcile(store,project_id,*,cli):
    root=store.path.parent/'projects'/project_id
    with project_lease(root/'controller.lock'):
        return await _reconcile_owned(store,project_id,cli=cli)


async def _reconcile_owned(store,project_id,*,cli):
    spec=store.project(project_id)['contract'];root=store.path.parent/'projects'/project_id
    report={'project_id':project_id,'reconciled':[],'held':[],'native_model_turns':0}
    client=JsonRpcClient((cli,'app-server','--stdio'))
    try:
        await client.start();await client.initialize()
        status=store.status(project_id)
        modules={row['owner']:row for row in status['modules']}
        latest_work={}
        active_owners={row['actor_id'] for row in status['operations'] if row['status'] in {'intent','running','unknown','stopping'}}
        for row in status['operations']:
            if row['kind']=='work':latest_work[row['actor_id']]=row['id']
        for operation in status['operations']:
            unsettled=operation['status'] in {'intent','running','unknown','stopping'}
            module_state=modules.get(operation['actor_id'],{})
            publication_gap=(operation['status']=='done' and operation['kind']=='work'
                and module_state.get('state')=='working' and operation['actor_id'] not in active_owners
                and latest_work.get(operation['actor_id'])==operation['id'])
            if not unsettled and not publication_gap:continue
            actor=store.actor(operation['actor_id'])
            if operation['kind']=='binding' or not actor['thread_id'] or not operation['remote_id']:
                report['held'].append({'operation':operation['id'],'reason':'unacknowledged root/turn; reservation held, no blind retry'})
                continue
            response=await client.call('thread/read',{'threadId':actor['thread_id'],'includeTurns':True})
            thread=response['thread']
            if thread.get('sessionId')!=actor['thread_id'] or thread.get('id')!=actor['thread_id']:
                raise ConflictError('Recovery changed independent native identity')
            turn=next((item for item in thread.get('turns',[]) if item['id']==operation['remote_id']),None)
            if not turn or turn.get('status') not in {'completed','failed','interrupted'}:
                report['held'].append({'operation':operation['id'],'reason':'native work is active or terminal evidence unavailable'})
                continue
            terminal='done' if turn['status']=='completed' else 'failed'
            project_state=store.project(project_id)['state']
            if project_state in {'cancelling','cancelled'}:terminal='cancelled'
            producer=None
            if terminal=='done' and actor['kind']=='module' and operation['kind']=='work':
                finals=[item.get('text','') for item in turn.get('items',[]) if item.get('type')=='agentMessage' and item.get('phase')!='commentary']
                try:producer=json.loads(finals[-1]) if finals else None
                except (ValueError,TypeError):producer=None
                if not isinstance(producer,dict) or producer.get('status') not in {'submitted','blocked'}:
                    report['held'].append({'operation':operation['id'],'reason':'completed native turn lacks a valid producer receipt; no inferred submission'})
                    continue
                if producer['status']=='blocked':terminal='failed'
            if unsettled:
                store.settle(operation['id'],state=terminal,evidence='persisted acknowledged native terminal turn and producer receipt; old activation namespace fenced')
            elif terminal!='done':
                report['held'].append({'operation':operation['id'],'reason':'settled completion contradicts producer/lifecycle outcome; no submission or counter rewrite'})
                continue
            # A completed producer can have crashed after turn completion but
            # before sealing/submission. Reconstruct only its original bound
            # workspace and immutable input view; do not rerun the model.
            payload=json.loads(operation['payload'])
            if terminal=='done' and actor['kind']=='module' and operation['kind']=='work':
                with store.connect() as con:
                    binding=con.execute("SELECT payload FROM operation WHERE actor_id=? AND kind='binding' AND at<=? ORDER BY at DESC LIMIT 1",(actor['id'],operation['at'])).fetchone()
                if not binding:raise ConflictError('No admitted activation workspace')
                workspace=Path(json.loads(binding['payload'])['workspace'])
                if root/'workspaces' not in workspace.parents:raise ConflictError('Recovery workspace escaped project ownership')
                module=next(item for item in spec.modules if item.key==actor['module_key'])
                inputs=payload['inputs']
                from .controller import Controller
                context=Controller(store,project_id,cli=cli).input_context(module,inputs)
                immutable_context=context_hashes(context,module.paths)
                artifact=capture(workspace,root/'public'/'candidates',module.paths,
                    bindings={'operation':operation['id'],'module':module.key,'inputs':inputs,'mandate_revision':payload['module_revision'],
                              'project_spec':spec.fingerprint,'input_context':str(context)},
                    baseline_files=[name for name in file_names(root/'public'/'base') if owned_path(name,module.paths)],context_inputs=immutable_context)
                import uuid
                candidate=uuid.uuid4().hex
                controller=Controller(store,project_id,cli=cli)
                checks=await controller.validate_module(module,Path(artifact['path']),context,root/'public'/'validation'/candidate)
                verify(Path(artifact['path']),module.paths,expected_hash=artifact['tree_hash'])
                principal=store.bind(actor['id'],actor['thread_id'],actor['session_id'])
                store.submit(principal,candidate,tree_hash=artifact['tree_hash'],snapshot=artifact['path'],inputs=inputs,
                    validation=checks,summary='Recovered completed native module: '+str(producer.get('summary',''))+'; actual independent checks',operation_id=operation['id'])
                # Review will attach bound persisted command evidence under the
                # resumed Master. This recovery does not launch a model turn.
                report['reconciled'].append({'operation':operation['id'],'state':terminal,'candidate':candidate})
            else:report['reconciled'].append({'operation':operation['id'],'state':terminal})
        store.finalize_cancellation(project_id)
        return report
    finally:await client.close()
