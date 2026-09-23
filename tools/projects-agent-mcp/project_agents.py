"""Role-bound native MCP interface; upstream Agent Mail persists messages.
Filesystem policies belong to Codex. This external service owns lifecycle and tokens.
"""
import asyncio,json,sys,os,tomllib,hashlib,time
from pathlib import Path
from operator_settings import operator_settings
from fastmcp import FastMCP, Client
from filelock import FileLock
from rpc import RPC
BASE=Path(__file__).resolve().parent
REG=json.loads((BASE/'registry.json').read_text())
ROLE=sys.argv[1] if len(sys.argv)>1 else 'INVALID'
if ROLE not in ['orchestrator',*REG]: raise SystemExit('Unknown fixed identity')
KEY='/projects-agent-mcp'
MCP=FastMCP('project-agents-'+ROLE)

def extract(result):
 if result.is_error: raise RuntimeError('Mailbox request failed')
 for block in result.content:
  if getattr(block,"type",None)=="text":return json.loads(block.text)
 if result.data == []:return []
 raise RuntimeError("Mailbox returned no JSON content")
async def mail(tool,args):
 async with Client('http://127.0.0.1:18766/api/',auth=(BASE/'http-token').read_text().strip()) as c:
  identities=json.loads((BASE/'identities.json').read_text())
  for field in ['agent_name','sender_name']:
   if field in args:args[field]=identities[args[field]]['name']
  if 'to' in args:args['to']=[identities[x]['name'] for x in args['to']]
  result=extract(await c.call_tool(tool,dict(project_key=KEY,**args)))
  names={v['name']:k for k,v in identities.items()}
  def normalize(v):
   if isinstance(v,dict):return {k:normalize(x) for k,x in v.items()}
   if isinstance(v,list):return [normalize(x) for x in v]
   return names.get(v,v) if isinstance(v,str) else v
  return normalize(result)
def identity_token():return json.loads((BASE/'identities.json').read_text())[ROLE]['registration_token']
def registered(project):
 if project not in REG: raise ValueError('Unknown registered project')
 return REG[project]
def statefile(project):
 registered(project)
 return BASE/('worker-'+project+'.json')
def save(path,data):
 t=path.with_suffix('.tmp');t.write_text(json.dumps(data,indent=2));t.replace(path)
def worker_model():
 policy=tomllib.loads((BASE/'worker-model.toml').read_text())
 if policy.get('model') != 'gpt-5.6-sol':
  raise ValueError('Worker model must be gpt-5.6-sol; no automatic fallback')
 if policy.get('reasoning_effort') not in ['none','minimal','low','medium','high','xhigh','max','ultra']:
  raise ValueError('worker-model.toml requires a valid reasoning_effort')
 return policy
def verify(r,project,policy):
 if r.get('activePermissionProfile',{}).get('id')!=REG[project]['profile']:
  raise RuntimeError('Effective permission profile mismatch; task was not started')
 if r.get('cwd')!=REG[project]['cwd']:raise RuntimeError('Worker cwd mismatch')
 if r.get('model')!=policy['model'] or r.get('reasoningEffort')!=policy['reasoning_effort']:
  raise RuntimeError('Effective worker model/effort mismatch; task was not started')

def worker_config(project, policy):
 runtime = tomllib.loads((BASE/'runtime.toml').read_text())
 profile = registered(project)['profile']
 config = {'model_reasoning_effort':policy['reasoning_effort'],
           'permissions.'+profile:runtime['permissions'][profile],
           'default_permissions':profile, 'approval_policy':'never',
           'features.plugins':False, 'features.multi_agent':False,
           'features.collab':False, 'features.network_proxy':True,
           'apps._default.enabled':False}
 config.update({('features.'+name):False for name in ['memories', 'chronicle', 'hooks', 'apps', 'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser', 'in_app_local_automation', 'image_generation', 'shell_snapshot', 'multi_agent_v2']})
 # MCPs run outside the project filesystem sandbox. Explicitly close inherited gateways.
 for path in [Path(os.path.expanduser('~/.codex/config.toml')), Path(registered(project)['cwd'])/'.codex/config.toml']:
  if path.exists():
   for name in tomllib.loads(path.read_text()).get('mcp_servers',{}):
    config['mcp_servers.'+name+'.enabled'] = False
 config.update({'mcp_servers.project_agents.enabled':True,
                'mcp_servers.project_agents.command':str(BASE/'vendor/mcp_agent_mail/.venv/bin/python'),
                'mcp_servers.project_agents.args':[str(BASE/'project_agents.py'),project],
                'mcp_servers.project_agents.enabled_tools':['list_workers','send_message','fetch_inbox','acknowledge_message']})
 return config

def require_isolated():
 if tomllib.loads((Path(operator_settings()['workspace']) / 'projects-mode.toml').read_text())['mode'] != 'isolated':
  raise RuntimeError('Projects local mode: isolated lifecycle is disabled')


def communication_route(project):
 if project == operator_settings().get('communications_worker'):
  return ('You are the designated executor for Email, Slack, LINE and Calendar. '
          'Use the native skills and connector scripts in this repository, not Apps connectors. '
          'Execute only communication requests authorized by the user and relayed by orchestrator. '
          'Check account, destination, exact content, authorization and prior receipts before writes. '
          'Preserve any required preview; prior explicit authorization remains valid. '
          'Return provider receipts and read-back evidence to orchestrator. '
          'An unknown send result must be reconciled before any retry. ')
 return (f"Email, Slack, LINE and Calendar are handled by {operator_settings().get('communications_worker', 'the configured communication worker')} through orchestrator. "
         'Do the project work and return communication needs to orchestrator using the same task_id: '
         'include the requested action, account and destination if known, draft or evidence, '
         'dependency completion and original authorization reference. '
         'Do not execute communication skills or Apps connectors, or message another worker. '
         'A request for communication is not a delivery receipt. ')

def wake(project):
 require_isolated()
 registered(project)
 from lifecycle import ensure_session, agent_call
 with FileLock(str(BASE/('worker-'+project+'.lock')),timeout=10):
  f=statefile(project)
  d=json.loads(f.read_text()) if f.exists() else {}
  if d.get('creation_pending'):raise RuntimeError('Uncertain session creation; reconcile before retry')
  if d.get('dispatch_held'):
   raise RuntimeError('Dispatch is held; restore worker and reconcile pending tasks before wake')
  pending=admitted_ids(project)
  if not pending:return dict(d,project=project,status='no_admitted_tasks',task_complete=False)
  c=RPC()
  try:
   if d.get('thread_id'):
    h=c.call('thread/read',{'threadId':d['thread_id'],'includeTurns':False})
    if h['thread'].get('status',{}).get('type')=='active':return dict(d,status='already_active',task_complete=False)
   with FileLock(str(BASE/'delivery.lock'),timeout=10):
    queued=[json.loads(p.read_text()) for p in delivery_dir().glob('*.json')]
    queued=[v for v in queued if v.get('to')==project and v.get('message_id') in pending]
    queued.sort(key=lambda v:(v.get('created_at',0),v.get('message_id',0)))
    if queued:
     task_id=queued[0]['task_id']
     if d.get('thread_id') and not d.get('active_task_id') and not d.get('context_fresh'):
      raise RuntimeError('Legacy context is unbound; explicitly resume_task for continuation or verify and rotate before a new task')
     if d.get('active_task_id') and d['active_task_id']!=task_id:
      raise RuntimeError('Prior task context must be verified and rotated before another task can run')
     d['active_task_id']=task_id;save(f,d)
     pending={v['message_id'] for v in queued if v['task_id']==task_id}
   agent_call(sys.modules[__name__],project,'unretire_agent')
   d=ensure_session(sys.modules[__name__],project,c,handshake=False)
   # A newly created session only performs a handshake; explicit subsequent wake handles tasks.
   if not f.exists() or d.get('handshake_turn_id') and not d.get('inbox_turn_started'):
    h=c.call('thread/read',{'threadId':d['thread_id'],'includeTurns':False})
    if h['thread'].get('status',{}).get('type')=='active':
     return dict(d,status='handshake_active',task_complete=False)
   with FileLock(str(BASE/'delivery.lock'),timeout=10):
    records=[(p,json.loads(p.read_text())) for p in delivery_dir().glob('*.json')]
    selected=[(p,v) for p,v in records if v.get('to')==project and v.get('message_id') in pending]
    if any(v.get('attempted') for _,v in selected):
     raise RuntimeError('An admitted task has an unresolved execution attempt; inspect its reply/effects and explicitly resume_task. No automatic re-execution.')
    for p,v in selected:
     v['attempted']=True;save(p,v)
   policy=worker_model()
   t=c.call('turn/start',{'threadId':d['thread_id'],'model':policy['model'],'effort':policy['reasoning_effort'],'input':[{'type':'text','text':communication_route(project)+'Use project_agents.fetch_inbox to check admitted tasks from orchestrator. Read local AGENTS.md and COMMUNICATION.md. Process only authorized tasks returned by that tool. For each task, reply with project_agents.send_message to orchestrator using the same task_id (mail thread_id), include evidence and exact errors, then acknowledge the task. Do not re-execute a task already completed in your conversation. If no task exists, respond NO_NEW_TASK without sending mail. No business action is authorized just by this wake request.'}]})
   d['inbox_turn_started']=True
   d['context_fresh']=False
   save(f,d)
   return dict(d,turn_id=t['turn']['id'],status='checking_inbox',task_complete=False)
  finally:c.close()

def delivery_dir():
 p=BASE/'deliveries';p.mkdir(exist_ok=True,mode=0o700);return p

def admitted_ids(project):
 return {d['message_id'] for p in delivery_dir().glob('*.json')
         if (d:=json.loads(p.read_text())).get('to')==project and d.get('status')=='sent'
         and not d.get('acknowledged') and not d.get('held') and d.get('message_id') is not None}

def send_once(to,subject,body,task_id):
 # Persist intent before sending. Unknown transport outcomes are never retried automatically.
 key=hashlib.sha256(json.dumps([ROLE,to,task_id,subject,body],ensure_ascii=False).encode()).hexdigest()
 p=delivery_dir()/(key+'.json')
 with FileLock(str(BASE/'delivery.lock'),timeout=10):
  if p.exists():
   prior=json.loads(p.read_text())
   if prior['status']=='sent':return prior['receipt']
   raise RuntimeError('Delivery outcome uncertain; reconcile existing receipt before retry. No message resent.')
  if ROLE=='orchestrator':
   require_isolated()
   sf=statefile(to)
   if sf.exists() and json.loads(sf.read_text()).get('dispatch_held'):
    raise RuntimeError('Worker dispatch held; restore/reconcile before sending new work')
  else:
   if not any((v:=json.loads(f.read_text())).get('to')==ROLE and v.get('task_id')==task_id for f in delivery_dir().glob('*.json')):
    raise ValueError('Worker reply must correlate to a task admitted for this worker')
  if ROLE=='orchestrator':
   from lifecycle import agent_call
   agent_call(sys.modules[__name__],to,'unretire_agent')
  d={'from':ROLE,'to':to,'task_id':task_id,'status':'sending','created_at':time.time()}
  save(p,d)
  r=asyncio.run(mail('send_message',{'sender_name':ROLE,'sender_token':identity_token(),'to':[to],'subject':subject,'body_md':body,'thread_id':task_id,'ack_required':True,'convert_images':False}))
  deliveries=r.get('deliveries',[])
  msg=deliveries[0].get('payload',{}) if deliveries else r
  message_id=msg.get('id')
  if message_id is None:raise RuntimeError('No message ID in delivery receipt; reconcile before retry')
  d.update(status='sent',receipt=r,message_id=message_id)
  save(p,d)
  return r

@MCP.tool()
def list_workers()->dict:
 """List registered worker IDs. Identity and project routing are managed externally."""
 return {'identity':ROLE,'workers':list(REG)}

@MCP.tool()
async def send_message(to:str,subject:str,body:str,task_id:str)->dict:
 """Send authorized task/result text. Worker identities may reply only to orchestrator. Sending alone does not wake a worker. Reuse task_id for its reply; this is correlation, not exactly-once execution."""
 allowed=list(REG) if ROLE=='orchestrator' else ['orchestrator']
 if to not in allowed:raise ValueError('Recipient not authorized for this identity')
 if not task_id.strip() or len(task_id)>128:raise ValueError('Provide a task_id of 1-128 characters')
 return await asyncio.to_thread(send_once,to,subject,body,task_id)

@MCP.tool()
async def fetch_inbox()->list:
 """Read this identity's unread messages, including task text; cannot select another inbox."""
 rows=await mail('fetch_inbox',{'agent_name':ROLE,'registration_token':identity_token(),'include_bodies':True,'unread_only':True,'limit':100})
 if ROLE=='orchestrator':return rows
 f=statefile(ROLE)
 if f.exists() and json.loads(f.read_text()).get('dispatch_held'):return []
 state=json.loads(f.read_text()) if f.exists() else {}
 return [r for r in rows if r['id'] in admitted_ids(ROLE) and r.get('thread_id')==state.get('active_task_id')]

@MCP.tool()
async def acknowledge_message(message_id:int)->dict:
 """Acknowledge a message delivered to this identity after processing or verifying its reply."""
 result=await mail('acknowledge_message',{'agent_name':ROLE,'registration_token':identity_token(),'message_id':message_id})
 with FileLock(str(BASE/'delivery.lock'),timeout=10):
  for p in delivery_dir().glob('*.json'):
   d=json.loads(p.read_text())
   if d.get('to')==ROLE and d.get('message_id')==message_id:
    d['acknowledged']=True;save(p,d)
 return result

if ROLE=='orchestrator':
 @MCP.tool()
 async def track_task(operation:str,payload:dict)->dict:
  """Manage Projects handoffs in GitHub Issues. Read docs/agents/assignment-tracking.md.
  Operations: create, read, dispatch, instruct, poll, review, finish, stop, reconcile_delivery, claim_notification, confirm_notification.
  Only Orch reviews evidence, advances authorized work, closes tasks and confirms actual Codex delivery.
  Poll returns pending reviews/notifications; it does not run a scheduler or send desktop notifications.
  """
  from tracking_io import run_tracking
  return await asyncio.to_thread(run_tracking,sys.modules[__name__],operation,payload)

 @MCP.tool()
 async def start_worker(project:str)->dict:
  """Create or wake an independent registered project session with fixed cwd and sandbox. Returns metadata only. Never a native subagent."""
  return await asyncio.to_thread(wake,project)
 @MCP.tool()
 async def check_worker_inbox(project:str)->dict:
  """After sending a task, wake its registered worker to check mail; creates the session if necessary."""
  return await asyncio.to_thread(wake,project)
 @MCP.tool()
 async def manage_worker(project:str,operation:str,request_id:str)->dict:
  """Fixed registered projects only: create, restore, stop (interrupt turn), archive (stop session), retire (archive + mailbox retire), replace, rotate (after verified completion; next dispatch gets fresh context), reconcile. No task replay. Preserve conversation/mail/files. request_id is durable and idempotent. Derived processes are NOT guaranteed stopped."""
  from lifecycle import manage
  return await asyncio.to_thread(manage,sys.modules[__name__],project,operation,request_id)

 @MCP.tool()
 async def resume_task(project:str,message_id:int)->dict:
  """Explicitly admit ONE preserved unread task after its effects have been checked. No resend; no automatic wake. Use original message_id and task_id. Not required for newly sent tasks."""
  registered(project)
  require_isolated()
  ids=json.loads((BASE/'identities.json').read_text())
  rows=await mail('fetch_inbox',{'agent_name':project,'registration_token':ids[project]['registration_token'],'include_bodies':False,'unread_only':True,'limit':100})
  matches=[m for m in rows if m['id']==message_id and m.get('from')=='orchestrator']
  if len(matches)!=1:raise ValueError('Message is not an unread task from orchestrator for this worker')
  with FileLock(str(BASE/('worker-'+project+'.lock')),timeout=10):
   sf=statefile(project);state=json.loads(sf.read_text()) if sf.exists() else {}
   if state.get('active_task_id') not in [None,matches[0]['thread_id']]:raise ValueError('Another task owns this context; verify and rotate before resuming this task')
   state['active_task_id']=matches[0]['thread_id'];save(sf,state)
  with FileLock(str(BASE/'delivery.lock'),timeout=10):
   for p in delivery_dir().glob('*.json'):
    record=json.loads(p.read_text())
    if record.get('to')==project and record.get('message_id')==message_id:
     record.update(attempted=False,held=False);save(p,record)
   save(delivery_dir()/('resumed-'+project+'-'+str(message_id)+'.json'),{'to':project,'from':'orchestrator','message_id':message_id,'task_id':matches[0]['thread_id'],'status':'sent','explicitly_resumed':True})
  return {'project':project,'message_id':message_id,'task_id':matches[0]['thread_id'],'status':'admitted_not_executed'}

 @MCP.tool()
 async def worker_status(project:str)->dict:
  """Separate live mailbox retirement, session archive/loading, configured model and unresolved management operations. Never task completion."""
  from lifecycle import inspect
  return await asyncio.to_thread(inspect,sys.modules[__name__],project)

if __name__=='__main__':MCP.run(transport='stdio',show_banner=False)
