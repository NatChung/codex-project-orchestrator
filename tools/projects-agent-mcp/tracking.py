"""Durable Orch coordination; GitHub comments are the recovery journal.

All callers on this host share a lock. The issue body is a repairable projection,
not the commit point. Worker reports remain unacknowledged until Orch review.
"""
import contextlib
import fcntl
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MARKER = "<!-- projects-orchestration-v1\n"
END = "\n-->"
TERMINAL = {"已完成", "已取消"}


def snapshot(text):
    if MARKER not in text:
        return None
    raw = text.rsplit(MARKER, 1)[1].split(END, 1)[0]
    return json.loads(raw)


def encoded(state):
    # Escape HTML delimiters so user-supplied text cannot end the snapshot.
    durable = {k: v for k, v in state.items() if not k.startswith('_')}
    result = MARKER + json.dumps(durable, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e") + END
    if len(result) > 50000:
        raise ValueError('Task journal exceeds safe GitHub size; split independent work into child issues')
    return result


def inactive(state):
    return state['status'] in TERMINAL or state.get('tracking_stopped') or state.get('_closed_externally')


def render(state):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    def timestamp(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')
    lines = [f"# [交辦] {state['title']}", "", "## 交辦",
             f"- 原始要求：{state['request']}", f"- 授權範圍：{state['scope']}",
             f"- 完成條件：{state['done_when']}", f"- 來源：{state['source']}",
             f"- 原票：{', '.join(state['links'])}", "", "## 最新協調狀態",
             f"- 狀態：{state['status']}", f"- 最近更新：{timestamp(state['updated_at'])}",
             f"- 下一步：{state.get('next_step', '')}",
             f"- 結案依據：{state.get('completion_evidence', '尚未結案')}",
             "", "## Worker 分工", "| Project | task_id | 分工 | 狀態 | 回報期限（UTC） |",
             "| --- | --- | --- | --- | --- |"]
    for a in state['assignments'].values():
        lines.append("| " + " | ".join(cell(a[k]) for k in ['project', 'task_id', 'body', 'status']) + " | " + timestamp(a['due']) + " |")
    lines += ["", "## 追蹤", "- 檢查間隔：10 分鐘；逾時追問後等 20 分鐘。",
              "- 完整核對、事件與通知收據見下方機器紀錄及留言。", "", encoded(state)]
    return "\n".join(lines)


class Tracker:
    def __init__(self, issues, mail, state_dir, clock=time.time):
        self.issues, self.mail, self.clock = issues, mail, clock
        self.state_dir = Path(state_dir)

    @contextlib.contextmanager
    def locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "tracking.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _load(self, number):
        issue = self.issues.read(number)
        state = snapshot(issue['body'])
        if not state:
            raise ValueError("Not a tracked Projects issue")
        for comment in issue['comments']:
            newer = snapshot(comment['body'])
            if newer and newer['key'] == state['key'] and newer['revision'] > state['revision']:
                state = newer
        state['issue'] = number
        state['url'] = issue['html_url']
        state['_closed_externally'] = issue['state'] == 'closed' and state['status'] not in TERMINAL
        state['_projection_stale'] = (issue['body'] != render(state) or
                                      (state['status'] in TERMINAL and issue['state'] != 'closed'))
        return state

    def _project(self, state):
        self.issues.project(state['issue'], render(state), state['status'] in TERMINAL or bool(state.get('_closed_externally')))

    def _commit(self, state, event, description):
        if event in state['events']:
            self._project(state)
            return
        if not inactive(state):
            for report in state['reports'].values():
                assignment = state['assignments'][report['message']['thread_id']]
                if not report['review'] and report['message']['id'] > assignment.get('last_applied_report', 0):
                    assignment['status'] = '待 Orch 核對'
            assignments = list(state['assignments'].values())
            statuses = {a['status'] for a in assignments}
            priorities = [('待 Nat 決策', {'待 Nat 決策', '失敗'}),
                          ('回報逾時', {'回報逾時'}),
                          ('待 Orch 核對', {'待 Orch 核對', '送達待確認'})]
            for aggregate, members in priorities:
                if statuses & members:
                    state['status'] = aggregate
                    state['next_step'] = '；'.join(a['task_id'] + '：' + a.get('next_step', a['status'])
                                                  for a in assignments if a['status'] in members)
                    break
            else:
                state['status'] = '執行中' if assignments else '待派工'
                if statuses == {'已完成'}:
                    state['next_step'] = 'Orch 核對整體完成條件並結案'
        state['revision'] += 1
        state['events'].append(event)
        state['updated_at'] = self.clock()
        self.issues.comment(state['issue'], description + "\n\n" + encoded(state))
        self._project(state)

    def create(self, key, title, request, scope, done_when, source, links):
        if not all(isinstance(v, str) and v.strip() for v in [key, title, request, scope, done_when, source]):
            raise ValueError("Request key, title, request, scope, completion criteria and source are required")
        with self.locked():
            for number in self.issues.list():
                state = self._load(number)
                if state['key'] == key:
                    return state
            state = dict(key=key, title=title, request=request, scope=scope, done_when=done_when,
                         source=source, links=links, status="待派工", assignments={}, reports={},
                         notifications={}, events=[], revision=0, updated_at=self.clock())
            number = self.issues.create("[交辦] " + title, render(state))
            return self._load(number)

    def read(self, number):
        with self.locked():
            return self._load(number)

    def dispatch(self, number, project, task_id, body, due):
        if not task_id.strip() or len(task_id) > 128 or not body.strip() or due <= self.clock():
            raise ValueError("A unique task_id, authorized body and future deadline are required")
        with self.locked():
            state = self._load(number)
            if inactive(state):
                raise ValueError("Task is closed")
            for other in self.issues.list():
                existing = self._load(other)['assignments'].get(task_id)
                if existing:
                    if other != number or existing['project'] != project or existing['body'] != body:
                        raise ValueError("task_id already belongs to a different assignment")
                    return state
            a = dict(project=project, task_id=task_id, body=body, due=due,
                     status="送達待確認", wake_pending=False, ping_at=None, receipt=None,
                     pending_send='dispatch:' + task_id)
            state['assignments'][task_id] = a
            state['next_step'] = "確認派工送達；結果不明時禁止自動重送"
            self._commit(state, 'dispatch-claim:' + task_id, '派工已登記，準備送出：' + task_id)
            # A crash after this claim is deliberately NOT retried: mail has no
            # exactly-once key. An Orch must reconcile delivery before proceeding.
            receipt = self.mail.send(project, task_id, body + f"\n\n交辦：{state['url']}\n下次回報期限 Unix UTC：{due}")
            a.update(receipt=receipt, status="執行中", wake_pending=True, pending_send=None)
            state.update(status="執行中", next_step="等待 worker 回報")
            self._commit(state, 'dispatch-sent:' + task_id, '派工送達：' + task_id)
            self._wake(state, a)
            return state

    def _wake(self, state, assignment):
        if assignment['wake_pending']:
            self.mail.wake(assignment['project'])
            assignment['wake_pending'] = False
            self._commit(state, 'wake:' + assignment['task_id'] + ':' + str(state['revision']), '已請 worker 查收：' + assignment['task_id'])

    def instruct(self, number, task_id, instruction_key, body, due):
        if not instruction_key.strip() or not body.strip() or due <= self.clock():
            raise ValueError("Follow-up requires a unique instruction key, body and future deadline")
        with self.locked():
            state = self._load(number)
            if inactive(state):
                raise ValueError("Task is no longer tracked")
            a = state['assignments'][task_id]
            event = 'instruction:' + task_id + ':' + instruction_key
            if event + ':claim' in state['events']:
                return state
            self._supersede_assignment_notifications(state, task_id)
            a.update(status='送達待確認', due=due, ping_at=None, pending_send=event)
            self._commit(state, event + ':claim', 'Orch 原授權內補充指令：' + body)
            receipt = self.mail.send(a['project'], task_id, body + f"\n\n交辦：{state['url']}\n下次回報期限 Unix UTC：{due}")
            a.update(status='執行中', receipt=receipt, wake_pending=True, pending_send=None)
            state.update(status='執行中', next_step='等待補充回報')
            self._commit(state, event + ':sent', '補充指令已送達：' + instruction_key)
            self._wake(state, a)
            return state

    def poll(self):
        with self.locked():
            states = {n: self._load(n) for n in self.issues.list()}
            for state in states.values():
                if state['_projection_stale'] and not state.get('_closed_externally'):
                    self._project(state)
            messages = self.mail.inbox() if states else []
            unknown = []
            for message in messages:
                matches = [s for s in states.values() if
                           message.get('thread_id') in s['assignments'] and
                           s['assignments'][message['thread_id']]['project'] == message.get('sender')]
                if len(matches) != 1:
                    unknown.append(message['id'])
                    continue
                state = matches[0]
                message_id = str(message['id'])
                prior = state['reports'].get(message_id)
                if prior and prior.get('review'):
                    self._project(state)
                    self.mail.acknowledge(message['id'])
                    continue
                if inactive(state):
                    continue
                if not prior:
                    state['reports'][message_id] = dict(message=message, received_at=self.clock(), review=None)
                    assignment = state['assignments'][message['thread_id']]
                    if message['id'] > assignment.get('last_applied_report', 0):
                        prefix = 'timeout:' + message['thread_id'] + ':'
                        for key, notification in state['notifications'].items():
                            if key.startswith(prefix) and not notification.get('receipt'):
                                notification['superseded_at'] = self.clock()
                        assignment.update(status="待 Orch 核對", ping_at=None)
                    state.update(status="待 Orch 核對", next_step="Orch 核對 worker 回報與證據")
                    self._commit(state, 'received:' + message_id, '收到 worker 回報，待核對：' + message_id)
            for state in states.values():
                if inactive(state):
                    continue
                for a in state['assignments'].values():
                    self._wake(state, a)
                    if a.get('pending_send'):
                        key = 'delivery:' + a['pending_send']
                        if key not in state['notifications']:
                            self._notify(state, key, '派工送達結果不明，需核對信箱；未自動重派：' + a['task_id'])
                            self._commit(state, key, '送達未確認，待通知 Nat：' + a['task_id'])
                        continue
                    if a['status'] not in {'執行中', '回報逾時'}:
                        continue
                    if self.clock() < a['due']:
                        continue
                    key = f"timeout:{a['task_id']}:{a['due']}"
                    if a['ping_at'] is None:
                        status = self.mail.status(a['project'])
                        a['ping_at'] = self.clock()
                        a['observed_worker'] = status
                        self._commit(state, key + ':claim', '回報逾時，已查執行狀態；登記一次追問：' + a['task_id'])
                        self.mail.send(a['project'], a['task_id'], '回報期限已過，請回報目前進度、證據及卡點。僅追問，不授權重做或擴大工作。')
                        self.mail.wake(a['project'])
                        self._commit(state, key + ':sent', '已追問一次：' + a['task_id'])
                    elif self.clock() >= a['ping_at'] + 1200 and key not in state['notifications']:
                        a['status'] = '回報逾時'
                        state.update(status='回報逾時', next_step='Nat 決定如何處理無回應的分工；未自動重派')
                        text = ('追問 20 分鐘後仍無回報：' if key + ':sent' in state['events']
                                else '逾時追問送達未確認，需核對信箱；未重送：')
                        self._notify(state, key, text + a['task_id'])
                        self._commit(state, key + ':notify', '回報逾時，待通知 Nat：' + a['task_id'])
            return dict(unknown_message_ids=unknown,
                        reviews=[dict(issue=s['issue'], message_id=int(mid), report=r['message'])
                                 for s in states.values() if not inactive(s)
                                 for mid, r in s['reports'].items() if not r['review']],
                        uncertain_notifications=[dict(issue=s['issue'], url=s['url'], key=k, **n)
                                       for s in states.values() for k, n in s['notifications'].items()
                                       if not n.get('receipt') and n.get('claim_token') and 'superseded_at' not in n
                                       and not s.get('tracking_stopped') and not s.get('_closed_externally')],
                        notifications=[dict(issue=s['issue'], url=s['url'], key=k, **n)
                                       for s in states.values() for k, n in s['notifications'].items()
                                       if not n.get('receipt') and not n.get('claim_token') and 'superseded_at' not in n
                                       and not s.get('tracking_stopped') and not s.get('_closed_externally')])

    def review(self, number, message_id, outcome, evidence, next_step, next_due=None, blocker_key=None):
        if outcome not in {'已完成', '執行中', '待 Nat 決策', '失敗'} or not evidence.strip() or not next_step.strip():
            raise ValueError("Review requires an outcome, verified evidence and next step")
        if outcome == '執行中' and (next_due is None or next_due <= self.clock()):
            raise ValueError("Continuing work needs its next reporting deadline")
        with self.locked():
            state = self._load(number)
            report = state['reports'][str(message_id)]
            if report['review']:
                self._project(state)
                self.mail.acknowledge(message_id)
                return state
            if inactive(state):
                raise ValueError("Task is no longer tracked")
            report['review'] = dict(outcome=outcome, evidence=evidence, next_step=next_step, at=self.clock())
            a = state['assignments'][report['message']['thread_id']]
            if message_id < a.get('last_applied_report', 0):
                report['review']['applied'] = False
                self._commit(state, 'review:' + str(message_id), f'Orch 核對舊回報 {message_id}，僅保存歷史證據：{evidence}')
                self.mail.acknowledge(message_id)
                return state
            report['review']['applied'] = True
            a['last_applied_report'] = message_id
            a.update(status=outcome, ping_at=None, next_step=next_step)
            if next_due is not None:
                a['due'] = next_due
            state.update(status='待 Nat 決策' if outcome in {'待 Nat 決策', '失敗'} else '執行中', next_step=next_step)
            if outcome in {'待 Nat 決策', '失敗'}:
                fingerprint = hashlib.sha256((outcome + ':' + (blocker_key or ' '.join(next_step.split()))).encode()).hexdigest()[:16]
                if a.get('blocker_fingerprint') != fingerprint:
                    self._supersede_assignment_notifications(state, a['task_id'])
                    a['blocker_generation'] = a.get('blocker_generation', 0) + 1
                    a['blocker_fingerprint'] = fingerprint
                    a['blocker_event'] = f"blocker:{a['task_id']}:{a['blocker_generation']}:{fingerprint}"
                self._notify(state, a['blocker_event'], outcome + '：' + next_step)
            else:
                self._supersede_assignment_notifications(state, a['task_id'])
            self._commit(state, 'review:' + str(message_id), f"Orch 已核對回報 {message_id}：{outcome}\n\n證據：{evidence}\n下一步：{next_step}")
            self.mail.acknowledge(message_id)
            return state

    def _notify(self, state, key, text):
        state['notifications'].setdefault(key, dict(text=text, created_at=self.clock(), receipt=None))

    def _supersede_assignment_notifications(self, state, task_id):
        a = state['assignments'][task_id]
        notification = state['notifications'].get(a.get('blocker_event'))
        if notification and not notification.get('receipt'):
            notification['superseded_at'] = self.clock()
        a['blocker_fingerprint'] = None

    def claim_notification(self, number, key):
        with self.locked():
            state = self._load(number)
            notification = state['notifications'][key]
            if notification.get('receipt') or notification.get('claim_token') or 'superseded_at' in notification or state.get('tracking_stopped') or state.get('_closed_externally'):
                return dict(claimed=False, reason='Already claimed, delivered, superseded or stopped; do not resend')
            token = str(uuid.uuid4())
            notification.update(claim_token=token, claimed_at=self.clock())
            self._commit(state, 'notification-claim:' + key, '已取得一次通知送出權；中斷後需查核收據，不自動重送：' + key)
            return dict(claimed=True, claim_token=token, text=notification['text'], url=state['url'])

    def confirm_notification(self, number, key, receipt, claim_token):
        if not isinstance(receipt, str) or not receipt.strip():
            raise ValueError("Require an actual Codex delivery receipt, not an intent to notify")
        with self.locked():
            state = self._load(number)
            notification = state['notifications'][key]
            if not claim_token or notification.get('claim_token') != claim_token:
                raise ValueError('Notification delivery claim does not match')
            if notification['receipt']:
                return state
            notification.update(receipt=receipt, delivered_at=self.clock())
            self._commit(state, 'notified:' + key, 'Codex 通知已送達：' + receipt)
            return state

    def finish(self, number, outcome, evidence):
        if outcome not in TERMINAL or not evidence.strip():
            raise ValueError("Require completion/cancellation and evidence")
        with self.locked():
            state = self._load(number)
            if state['status'] in TERMINAL:
                if state['status'] != outcome:
                    raise ValueError("Task already closed with a different outcome")
                self._project(state)
                return state
            if outcome == '已完成' and (not state['assignments'] or
                    any(a['status'] != '已完成' or a.get('pending_send') for a in state['assignments'].values()) or
                    any(not r['review'] for r in state['reports'].values())):
                raise ValueError("All assignments and reports must be verified before completion")
            for n in state['notifications'].values():
                if not n.get('receipt'):
                    n['superseded_at'] = self.clock()
            state.update(status=outcome, next_step='追蹤結束', completion_evidence=evidence)
            if outcome == '已完成':
                self._notify(state, 'completed', '交辦已完成：' + evidence)
            self._commit(state, 'finish', outcome + '：' + evidence)
            return state

    def stop(self, number, reason):
        if not reason.strip():
            raise ValueError('Require Nat stop instruction as evidence')
        with self.locked():
            state = self._load(number)
            state.update(tracking_stopped=True, next_step='已依 Nat 指示停止追蹤：' + reason)
            for n in state['notifications'].values():
                if not n.get('receipt'):
                    n['superseded_at'] = self.clock()
            self._commit(state, 'tracking-stopped', 'Nat 停止追蹤：' + reason)
            return state

    def reconcile_delivery(self, number, task_id, receipt, evidence):
        if not receipt or not evidence.strip():
            raise ValueError('Require externally verified delivery receipt and evidence')
        with self.locked():
            state = self._load(number)
            if inactive(state):
                raise ValueError('Task is no longer tracked')
            a = state['assignments'][task_id]
            if not a.get('pending_send'):
                return state
            event = 'delivery:' + a['pending_send']
            if event in state['notifications']:
                state['notifications'][event]['superseded_at'] = self.clock()
            a.update(status='執行中', receipt=receipt, pending_send=None, wake_pending=True)
            self._commit(state, event + ':reconciled', 'Orch 已核實送達：' + evidence)
            self._wake(state, a)
            return state
