"""Fixed-registry lifecycle broker. No caller-supplied paths, profiles or commands."""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from filelock import FileLock

ACTIONS = {'create', 'restore', 'stop', 'archive', 'retire', 'replace', 'reconcile', 'rotate'}

def missing_session(exc):
    text=str(exc).lower()
    return any(marker in text for marker in ['thread not found','no rollout found','thread does not exist'])


def agent_call(a, project, tool):
    identity = json.loads((a.BASE / 'identities.json').read_text())[project]
    args = {'agent_name': project, 'registration_token': identity['registration_token']}
    if tool == 'whois':
        args['include_recent_commits'] = False
    return asyncio.run(a.mail(tool, args))


def inspect(a, project, rpc=None):
    spec = a.registered(project)
    f = a.statefile(project)
    d = json.loads(f.read_text()) if f.exists() else {}
    result = {'project': project, 'cwd': spec['cwd'], 'profile': spec['profile'],
              'thread_id': d.get('thread_id'), 'session': 'not_created',
              'configured_model': a.worker_model(), 'mailbox': 'unknown',
              'dispatch': 'held' if d.get('dispatch_held') else 'enabled'}
    try:
        agent = agent_call(a, project, 'whois')
        result['mailbox'] = 'retired' if agent.get('retired_at') else 'active'
    except Exception as exc:
        result['mailbox_error'] = str(exc)
    if d.get('thread_id'):
        own = rpc is None
        rpc = rpc or a.RPC()
        try:
            t = rpc.call('thread/read', {'threadId': d['thread_id'], 'includeTurns': False})['thread']
            result.update(session=t.get('status', {}).get('type', 'unknown'),
                          archived='archived_sessions' in (t.get('path') or ''),
                          cwd_matches=t.get('cwd') == spec['cwd'])
        except Exception as exc:
            result.update(session='unavailable', session_error=str(exc))
        finally:
            if own:
                rpc.close()
    result['previous_sessions'] = d.get('previous_sessions', [])
    result['last_verified_model'] = d.get('model')
    result['pending_operations'] = [p.stem for p in (a.BASE / 'management').glob(project + '-*.json')
                                    if json.loads(p.read_text()).get('status') == 'in_progress']
    return result


def ensure_session(a, project, c, *, new=False, handshake=True):
    spec = a.registered(project)
    f = a.statefile(project)
    old = json.loads(f.read_text()) if f.exists() else {}
    if old.get('creation_pending'):
        raise RuntimeError('Uncertain session creation; maintenance reconciliation required')
    policy = a.worker_model()
    config = a.worker_config(project, policy)
    params = {'cwd': spec['cwd'], 'permissions': spec['profile'], 'approvalPolicy': 'never',
              'model': policy['model'], 'config': config,
              'developerInstructions': 'Independent isolated worker for ' + project + '. '
              'Use only your fixed project_agents mailbox. No native subagents. '
              'Shell uses login=false. Management recovery is not authorization to execute old tasks.'}
    if old.get('thread_id') and not new:
        t = c.call('thread/read', {'threadId': old['thread_id'], 'includeTurns': False})['thread']
        if t.get('cwd') != spec['cwd']:
            raise RuntimeError('Saved session cwd mismatch; explicit safe replacement required')
        if t.get('status', {}).get('type') == 'active':
            raise RuntimeError('Worker is active; stop it before changing lifecycle or policy')
        if 'archived_sessions' in (t.get('path') or ''):
            c.call('thread/unarchive', {'threadId': old['thread_id']})
        params['threadId'] = old['thread_id']
        r = c.call('thread/resume', params)
    else:
        params['historyMode'] = 'legacy'
        a.save(f, dict(old, creation_pending=True))
        r = c.call('thread/start', params)
    # Save the returned ID before verification: even a rejected runtime must remain recoverable.
    d = dict(old, project=project, thread_id=r['thread']['id'], cwd=r['cwd'],
             profile=r.get('activePermissionProfile'), model=r.get('model'),
             reasoning_effort=r.get('reasoningEffort'))
    if new or not old.get('thread_id'):d['context_fresh']=True
    a.save(f, d)
    a.verify(r, project, policy)
    if handshake and (new or not old.get('thread_id')):
        # Materialize the rollout without reading mail or executing any project work.
        turn = c.call('turn/start', {'threadId': d['thread_id'], 'model': policy['model'],
            'effort': policy['reasoning_effort'], 'input': [{'type': 'text', 'text':
            'Lifecycle handshake only. Do not call any tool, fetch mail, inspect files, or execute tasks. '
            'Reply WORKER_READY and stop. This is not a business assignment.'}]})
        d['handshake_turn_id'] = turn['turn']['id']
        a.save(f, d)
    return d


def interrupt(a, project, c, d):
    if not d.get('thread_id'):
        return {'turn': 'not_created'}
    try:
        t = c.call('thread/read', {'threadId': d['thread_id'], 'includeTurns': True})['thread']
    except RuntimeError as exc:
        if missing_session(exc):return {'session':'missing','original_thread_id':d['thread_id'],'derived_processes':'not_terminated_or_verified'}
        raise
    active = [v for v in t.get('turns', []) if v.get('status') == 'inProgress']
    for turn in active:
        c.call('turn/interrupt', {'threadId': d['thread_id'], 'turnId': turn['id']})
    return {'interrupt_requested': [v['id'] for v in active],
            'derived_processes': 'not_terminated_or_verified'}


def manage(a, project, operation, request_id):
    a.registered(project)
    a.require_isolated()
    if operation not in ACTIONS:
        raise ValueError('Unsupported lifecycle operation')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id):
        raise ValueError('request_id must contain 1-80 letters, digits, underscores or hyphens')
    directory = a.BASE / 'management'
    directory.mkdir(exist_ok=True, mode=0o700)
    journal = directory / (project + '-' + request_id + '.json')
    with FileLock(str(a.BASE / ('worker-' + project + '.lock')), timeout=10):
        if operation == 'reconcile':
            if not journal.exists():raise ValueError('No lifecycle journal with this request_id')
            event=json.loads(journal.read_text())
            f=a.statefile(project); current=json.loads(f.read_text()) if f.exists() else {}
            if current.get('creation_pending'):
                raise RuntimeError('Creation response lost without a thread ID; operator must identify the created session. No automatic retry is safe.')
            observed=inspect(a,project)
            if observed['session']=='unavailable':raise RuntimeError('Session still unavailable; reconciliation did not clear the pending operation')
            event.update(status='reconciled',observed=observed)
            a.save(journal,event)
            return {'project':project,'operation':'reconcile','observed':observed,'task_execution':'not_started','next_step':'Use a new request_id if further lifecycle repair is needed; no mutation replayed'}
        if journal.exists():
            event = json.loads(journal.read_text())
            if event['operation'] != operation:
                raise ValueError('request_id already used for another operation')
            if event['status'] == 'done':
                return event['result']
            raise RuntimeError('Previous lifecycle result uncertain; inspect saved state and journal. No retry or replacement started.')
        # Reject a different request ID after an unresolved management mutation too.
        pending = [p for p in directory.glob(project + '-*.json')
                   if json.loads(p.read_text()).get('status') == 'in_progress']
        if pending and operation not in {'stop','archive','retire'}:
            raise RuntimeError('Unresolved lifecycle operation; reconcile before another mutation')
        if operation == 'rotate':
            current=json.loads(a.statefile(project).read_text()) if a.statefile(project).exists() else {}
            task_id=current.get('active_task_id')
            unresolved=[v for p in a.delivery_dir().glob('*.json') if ((v:=json.loads(p.read_text())).get('to')==project or v.get('from')==project) and not v.get('acknowledged') and (not task_id or v.get('task_id')==task_id)]
            if unresolved:raise RuntimeError('Unacknowledged tasks or reports remain; verify/reconcile before context rotation')
            if inspect(a,project)['session']=='active':raise RuntimeError('Active worker cannot rotate before task verification')
        f = a.statefile(project)
        before = json.loads(f.read_text()) if f.exists() else {}
        event = {'project': project, 'operation': operation, 'request_id': request_id,
                 'status': 'in_progress', 'before': before, 'started_at': time.time()}
        a.save(journal, event)
        c = a.RPC()
        try:
            details = {}
            if operation in {'stop', 'archive', 'retire', 'replace', 'rotate'}:
                # Hold dispatch before interruption. All original files and mailbox records remain.
                before['dispatch_held'] = True
                if operation in {'stop','archive','retire','replace'}:
                    with FileLock(str(a.BASE/'delivery.lock'),timeout=10):
                        for path in a.delivery_dir().glob('*.json'):
                            delivery=json.loads(path.read_text())
                            if delivery.get('to')==project and not delivery.get('acknowledged'):
                                delivery['held']=True; a.save(path,delivery)
                a.save(f, before)
                details = interrupt(a, project, c, before)
            if operation in {'archive', 'retire', 'replace', 'rotate'} and before.get('thread_id') and details.get('session')!='missing':
                t = c.call('thread/read', {'threadId': before['thread_id'], 'includeTurns': False})['thread']
                if 'archived_sessions' not in (t.get('path') or ''):
                    c.call('thread/archive', {'threadId': before['thread_id']})
            if operation == 'rotate':
                history=before.get('previous_sessions',[])
                if before.get('thread_id'):history=history+[before['thread_id']]
                a.save(f,dict(before,thread_id=None,previous_sessions=history,dispatch_held=False,handshake_turn_id=None,inbox_turn_started=False,active_task_id=None))
                details.update(next_session='created_on_next_dispatch',mail_identity='unchanged')
            if operation == 'retire':
                agent_call(a, project, 'retire_agent')
            if operation in {'create', 'restore', 'replace'}:
                if operation == 'replace':
                    # Keep identity/mail/task relations; retain immutable mapping to every former session.
                    history = before.get('previous_sessions', [])
                    if before.get('thread_id'):
                        history = history + [before['thread_id']]
                    a.save(f, dict(before, previous_sessions=history, dispatch_held=True,active_task_id=None))
                agent_call(a, project, 'unretire_agent')
                d = ensure_session(a, project, c, new=operation == 'replace')
                # Recovery itself NEVER fetches pending mail. Replacement keeps old tasks held.
                d['dispatch_held'] = operation == 'replace'
                a.save(f, d)
                details.update(thread_id=d['thread_id'], handshake_turn_id=d.get('handshake_turn_id'))
            observed=inspect(a,project,c)
            observed['pending_operations']=[k for k in observed['pending_operations'] if k!=journal.stem]
            result = dict(project=project, operation=operation, request_id=request_id,
                          details=details, state=observed, task_execution='not_started')
            event.update(status='done', result=result, completed_at=time.time())
            a.save(journal, event)
            return result
        except Exception as exc:
            event['error'] = str(exc)
            a.save(journal, event)
            raise
        finally:
            c.close()
