"""External GitHub and native mailbox boundaries for Projects tracking."""
import asyncio
import json
import os
import subprocess
from operator_settings import operator_settings


class GitHubIssues:
    def __init__(self):
        settings = operator_settings()['tracking']
        self.repo = settings['repo']
        self.account = settings['account']
        token = subprocess.run(['gh', 'auth', 'token', '--user', self.account],
                               capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        self.env = dict(os.environ, GH_TOKEN=token)
        # Select an explicit token without changing gh's machine-global account.
        if self._api('user')['login'] != self.account:
            raise RuntimeError('GitHub identity mismatch')

    def _api(self, path, payload=None, pages=False):
        args = ['gh', 'api', path]
        if pages:
            args += ['--paginate', '--slurp']
        if payload is not None:
            args += ['--method', 'PATCH' if path.endswith('/body') else 'POST', '--input', '-']
            path = path.removesuffix('/body')
            args[2] = path
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                env=self.env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Do not include command environments, tokens, or unrestricted stderr.
            raise RuntimeError(f'GitHub request failed ({result.returncode}) for {path}')
        data = json.loads(result.stdout)
        return [item for page in data for item in page] if pages else data

    def list(self):
        return [i['number'] for i in self._api(f'repos/{self.repo}/issues?state=all&labels=orchestration&per_page=100', pages=True)
                if 'pull_request' not in i]

    def create(self, title, body):
        labels = self._api(f'repos/{self.repo}/labels?per_page=100', pages=True)
        if not any(label['name'] == 'orchestration' for label in labels):
            self._api(f'repos/{self.repo}/labels', dict(name='orchestration', color='5319E7', description='Orch 手動交辦追蹤'))
        return self._api(f'repos/{self.repo}/issues', dict(title=title, body=body, labels=['orchestration']))['number']

    def read(self, number):
        issue = self._api(f'repos/{self.repo}/issues/{int(number)}')
        if issue['user']['login'] != self.account or 'orchestration' not in [l['name'] for l in issue['labels']]:
            raise ValueError('Issue is not owned by this tracker identity')
        comments = self._api(f'repos/{self.repo}/issues/{int(number)}/comments?per_page=100', pages=True)
        issue['comments'] = [c for c in comments if c['user']['login'] == self.account]
        return issue

    def comment(self, number, body):
        self._api(f'repos/{self.repo}/issues/{int(number)}/comments', dict(body=body))

    def project(self, number, body, closed):
        self._api(f'repos/{self.repo}/issues/{int(number)}/body', dict(body=body, state='closed' if closed else 'open'))


class NativeMailbox:
    """Uses the existing role-bound adapter; no new tokens or socket permissions."""
    def __init__(self, adapter):
        self.adapter = adapter

    def inbox(self):
        messages = asyncio.run(self.adapter.mail('fetch_inbox', dict(agent_name='orchestrator',
            registration_token=self.adapter.identity_token(), include_bodies=True, unread_only=True, limit=100)))
        return [dict(m, sender=m['from']) for m in messages]

    def send(self, project, task_id, body):
        self.adapter.registered(project)
        return self.adapter.send_once(project, '[交辦追蹤] ' + task_id, body, task_id)

    def wake(self, project):
        return self.adapter.wake(project)

    def status(self, project):
        path = self.adapter.statefile(project)
        if not path.exists():
            return dict(project=project, status='not_started')
        data = json.loads(path.read_text())
        rpc = self.adapter.RPC()
        try:
            return rpc.call('thread/read', dict(threadId=data['thread_id'], includeTurns=False))['thread']['status']
        finally:
            rpc.close()

    def acknowledge(self, message_id):
        return asyncio.run(self.adapter.mail('acknowledge_message', dict(agent_name='orchestrator',
            registration_token=self.adapter.identity_token(), message_id=message_id)))


OPERATIONS = {'create', 'read', 'dispatch', 'instruct', 'poll', 'review', 'finish', 'stop', 'claim_notification', 'confirm_notification', 'reconcile_delivery'}


def run_tracking(adapter, operation, payload):
    if adapter.ROLE != 'orchestrator':
        raise PermissionError('Only orchestrator may manage central tracking')
    if operation not in OPERATIONS:
        raise ValueError('Unknown tracking operation')
    if operation == 'dispatch':
        adapter.registered(payload['project'])
    from tracking import Tracker
    tracker = Tracker(GitHubIssues(), NativeMailbox(adapter), adapter.BASE / 'tracking')
    return getattr(tracker, operation)(**payload)
