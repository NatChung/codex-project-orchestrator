import json,time
from pathlib import Path
from websockets.sync.client import unix_connect
LAB=Path(__file__).resolve().parent
class RPC:
 def __init__(self):
  self.ws=unix_connect(str(LAB/'app.sock'),uri='ws://localhost',max_size=30000000,compression=None);self.n=0
  self.call('initialize',{'clientInfo':{'name':'mcp-isolation-test','version':'1.0'},'capabilities':{'experimentalApi':True}})
  self.ws.send(json.dumps({'method':'initialized'}))
 def call(self,method,params):
  self.n+=1;n=self.n;self.ws.send(json.dumps({'id':n,'method':method,'params':params}))
  while True:
   r=json.loads(self.ws.recv(timeout=60))
   if r.get('id')==n:
    if 'error' in r:raise RuntimeError(r['error'])
    return r['result']
   with (LAB/'rpc-events.jsonl').open('a') as f:f.write(json.dumps(r)+'\n')
 def close(self):self.ws.close()
