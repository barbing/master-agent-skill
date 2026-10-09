"""Reconcile acknowledged terminal turns; never respawn an unknown dispatch."""
from contextlib import closing
from pathlib import Path
import json

from .artifacts import capture,context_hashes,file_names,owned_path,verify
from .contracts import ConflictError
from .native import JsonRpcClient, read_turn
from .lease import project_lease
from .claude_native import claude_trace_result


async def reconcile(store,project_id,*,cli):
    root=store.path.parent/'projects'/project_id
    with project_lease(root/'controller.lock'):
        return await _reconcile_owned(store,project_id,cli=cli)


def _decision(text):
    try:
        value=json.loads(text) if text else None
    except (ValueError,TypeError):
        return None
    if isinstance(value,dict) and value.get('decision') in {'approve','request_changes'} and isinstance(value.get('rationale'),str):
        return value
    return None


def _producer(text):
    try:
        value=json.loads(text) if text else None
    except (ValueError,TypeError):
        return None
    return value if isinstance(value,dict) and value.get('status') in {'submitted','blocked'} else None


async def _reconcile_owned(store,project_id,*,cli):
    project=store.project(project_id)
    spec,policy=project['contract'],project['policy']
    root=store.path.parent/'projects'/project_id
    report={'project_id':project_id,'reconciled':[],'held':[],'native_model_turns':0}
    client=None
    controller=None
    def get_controller():
        nonlocal controller
        if controller is None:
            from .controller import Controller
            controller=Controller(store,project_id,cli=cli)
        return controller
    try:
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
            provider=policy.runtime_for(spec,actor['module_key']).provider
            if provider=='claude':
                # The host wrote every stream line itself; no native call is needed.
                turn_status,final_text=claude_trace_result(root/'native-traces'/(operation['id']+'.stream.jsonl'))
                source='host stream trace'
            else:
                if client is None:
                    client=JsonRpcClient((cli,'app-server','--stdio'))
                    await client.start();await client.initialize()
                thread,turn,source=await read_turn(client,actor['thread_id'],operation['remote_id'])
                if thread.get('sessionId')!=actor['thread_id'] or thread.get('id')!=actor['thread_id']:
                    raise ConflictError('Recovery changed independent native identity')
                turn_status=turn.get('status') if turn else None
                finals=[item.get('text','') for item in (turn or {}).get('items',[]) if item.get('type')=='agentMessage' and item.get('phase')!='commentary']
                final_text=finals[-1] if finals else None
            abandoned=False
            admission=None
            if turn_status not in {'completed','failed','interrupted'}:
                if unsettled and operation.get('exit_observed'):
                    # The controller observed the owned process exit; the turn
                    # can produce no further effects and its workspace is fenced.
                    abandoned=True
                else:
                    report['held'].append({'operation':operation['id'],'reason':f'native work is active or terminal evidence unavailable ({source})'})
                    continue
            terminal='done' if turn_status=='completed' else 'failed'
            project_state=store.project(project_id)['state']
            if project_state in {'cancelling','cancelled'}:terminal='cancelled'
            producer=None
            if terminal=='done' and actor['kind']=='module' and operation['kind']=='work':
                producer=_producer(final_text)
                if producer is None:
                    report['held'].append({'operation':operation['id'],'reason':'completed native turn lacks a valid producer receipt; no inferred submission'})
                    continue
                if producer['status']=='blocked':terminal='failed'
            if unsettled:
                if terminal=='failed' and producer is None and actor['kind']=='module' and operation['kind']=='work':
                    # A native failure, interrupt or abandoned turn is not the
                    # owner's verdict: checkpoint its files and continue.
                    reason='transport_lost' if abandoned else 'native_failed'
                    checkpoint=None
                    with closing(store.connect()) as con:
                        binding=con.execute("SELECT payload FROM operation WHERE actor_id=? AND kind='binding' AND at<=? ORDER BY at DESC LIMIT 1",(actor['id'],operation['at'])).fetchone()
                    module=next((item for item in spec.modules if item.key==actor['module_key']),None)
                    if binding and module:
                        workspace=Path(json.loads(binding['payload'])['workspace'])
                        if root/'workspaces' in workspace.parents and workspace.is_dir():
                            payload=json.loads(operation['payload'])
                            control=get_controller()
                            context=control.input_context(module,payload['inputs'])
                            checkpoint=control.capture_checkpoint(workspace,module,operation['id'],payload['inputs'],context,payload['module_revision'])
                    from .controller import continuation_instructions
                    state=store.settle_interrupted(operation['id'],reason=reason,checkpoint=checkpoint,
                        evidence=f'reconciled {turn_status or "abandoned"} native turn ({source}); old activation namespace fenced',
                        instructions=continuation_instructions(reason),max_continuations=policy.max_continuations)
                    report['reconciled'].append({'operation':operation['id'],'state':'failed','module_state':state})
                    continue
                store.settle(operation['id'],state=terminal,evidence='persisted acknowledged native terminal turn and producer receipt; old activation namespace fenced')
                if terminal=='done' and actor['kind']=='master' and operation['kind']=='admission':
                    decision=_decision(final_text)
                    if decision and store.admission_state(project_id)=='pending':
                        # The persisted Master turn already decided; record it instead of spending a new turn.
                        store.record_admission(project_id,decision=decision['decision'],rationale=decision['rationale'],
                                               operation=operation['id'],source='reconciled')
                        admission=decision['decision']
                if producer and producer['status']=='blocked':
                    store.annotate_module(project_id,actor['module_key'],failure_kind='owner_blocked',
                        reason=str(producer.get('summary',''))+(' Issues: '+'; '.join(map(str,producer.get('issues') or [])) if producer.get('issues') else ''),
                        action='master_or_operator_decision_then_rearm')
            elif terminal!='done':
                report['held'].append({'operation':operation['id'],'reason':'settled completion contradicts producer/lifecycle outcome; no submission or counter rewrite'})
                continue
            # A completed producer can have crashed after turn completion but
            # before sealing/submission. Reconstruct only its original bound
            # workspace and immutable input view; do not rerun the model.
            payload=json.loads(operation['payload'])
            if terminal=='done' and actor['kind']=='module' and operation['kind']=='work':
                with closing(store.connect()) as con:
                    binding=con.execute("SELECT payload FROM operation WHERE actor_id=? AND kind='binding' AND at<=? ORDER BY at DESC LIMIT 1",(actor['id'],operation['at'])).fetchone()
                if not binding:raise ConflictError('No admitted activation workspace')
                workspace=Path(json.loads(binding['payload'])['workspace'])
                if root/'workspaces' not in workspace.parents:raise ConflictError('Recovery workspace escaped project ownership')
                module=next(item for item in spec.modules if item.key==actor['module_key'])
                inputs=payload['inputs']
                control=get_controller()
                context=control.input_context(module,inputs)
                immutable_context=context_hashes(context,module.paths)
                artifact=capture(workspace,root/'public'/'candidates',module.paths,
                    bindings={'operation':operation['id'],'module':module.key,'inputs':inputs,'mandate_revision':payload['module_revision'],
                              'project_spec':spec.fingerprint,'input_context':str(context)},
                    baseline_files=[name for name in file_names(root/'public'/'base') if owned_path(name,module.paths)],context_inputs=immutable_context)
                import uuid
                candidate=uuid.uuid4().hex
                checks=await control.validate_module(module,Path(artifact['path']),context,root/'public'/'validation'/candidate)
                verify(Path(artifact['path']),module.paths,expected_hash=artifact['tree_hash'])
                principal=store.bind(actor['id'],actor['thread_id'],actor['session_id'])
                store.submit(principal,candidate,tree_hash=artifact['tree_hash'],snapshot=artifact['path'],inputs=inputs,
                    validation=checks,summary='Recovered completed native module: '+str(producer.get('summary',''))+'; actual independent checks',operation_id=operation['id'])
                # Review will attach bound persisted command evidence under the
                # resumed Master. This recovery does not launch a model turn.
                report['reconciled'].append({'operation':operation['id'],'state':terminal,'candidate':candidate})
            else:report['reconciled'].append({'operation':operation['id'],'state':terminal,**({'admission':admission} if admission else {})})
        store.finalize_cancellation(project_id)
        return report
    finally:
        if client is not None:
            await client.close()
