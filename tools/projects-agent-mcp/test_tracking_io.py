"""Contract checks against fake gh responses, with no credentials or network."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tracking_io import GitHubIssues, NativeMailbox, run_tracking


class GitHubContractTests(unittest.TestCase):
    def test_explicit_identity_paginated_comments_and_patch(self):
        responses = [
            'fixture-token', {'login': 'example'},
            [[{'number': 2}], [{'number': 3}]],
            {'user': {'login': 'example'}, 'labels': [{'name': 'orchestration'}], 'body': 'body'},
            [[{'user': {'login': 'example'}, 'body': 'journal'}],
             [{'user': {'login': 'someone-else'}, 'body': 'untrusted snapshot'}]],
            {},
        ]
        requests = []
        def run(args, **kwargs):
            requests.append((args, kwargs))
            response = responses.pop(0)
            return subprocess.CompletedProcess(args, 0, response if isinstance(response, str) else json.dumps(response), '')
        with patch('tracking_io.operator_settings', return_value={'tracking': {'repo': 'example/coordination', 'account': 'example'}}), patch('tracking_io.subprocess.run', side_effect=run):
            github = GitHubIssues()
            self.assertEqual(github.list(), [2, 3])
            self.assertEqual(len(github.read(2)['comments']), 1)
            github.project(2, 'updated', False)
        args, options = requests[-1]
        self.assertEqual(args[2], 'repos/example/coordination/issues/2')
        self.assertIn('PATCH', args)
        self.assertEqual(json.loads(options['input'])['body'], 'updated')
        self.assertEqual(options['env']['GH_TOKEN'], 'fixture-token')

    def test_worker_identity_cannot_manage_central_tracking(self):
        with self.assertRaises(PermissionError):
            run_tracking(SimpleNamespace(ROLE='communications'), 'poll', {})

    def test_mailbox_normalizes_real_from_field_to_registered_sender(self):
        async def mail(tool, args):
            return [{'id': 144, 'from': 'communications', 'project_id': 1,
                     'thread_id': 'roundtrip-a', 'body_md': 'OK'}]
        adapter = SimpleNamespace(mail=mail, identity_token=lambda: 'fixture')
        result = NativeMailbox(adapter).inbox()
        self.assertEqual(result[0]['sender'], 'communications')


if __name__ == '__main__':
    unittest.main()
