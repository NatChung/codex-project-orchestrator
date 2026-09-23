"""Behavior tests at the agreed issue/mailbox/clock boundaries."""
import copy
import tempfile
import unittest
from pathlib import Path

from tracking import Tracker


class Issues:
    def __init__(self):
        self.items = {}
        self.fail_projection = False

    def list(self):
        return list(self.items)

    def create(self, title, body):
        number = len(self.items) + 1
        self.items[number] = {"number": number, "html_url": f"https://github.com/example/coordination/issues/{number}",
                              "body": body, "comments": [], "state": "open"}
        return number

    def read(self, number):
        return copy.deepcopy(self.items[number])

    def comment(self, number, body):
        self.items[number]["comments"].append({"body": body})

    def project(self, number, body, closed):
        if self.fail_projection:
            raise OSError("GitHub unavailable")
        self.items[number].update(body=body, state="closed" if closed else "open")


class Mail:
    def __init__(self):
        self.messages = []
        self.sent = []
        self.acked = []
        self.woken = []

    def inbox(self):
        return [m for m in self.messages if m["id"] not in self.acked]

    def send(self, project, task_id, body):
        self.sent.append((project, task_id, body))
        return {"id": len(self.sent)}

    def wake(self, project):
        self.woken.append(project)
        return {"status": "checking_inbox"}

    def status(self, project):
        return {"status": "active"}

    def acknowledge(self, message_id):
        self.acked.append(message_id)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.issues = Issues()
        self.mail = Mail()
        self.now = 1000
        self.tracker = Tracker(self.issues, self.mail, Path(self.tmp.name), lambda: self.now)

    def create(self, key="request-1"):
        return self.tracker.create(key, "查核 084", "查出差異來源", "唯讀", "提出有來源的結論", "codex task", [])

    def test_same_request_survives_restart_as_one_issue(self):
        first = self.create()
        restarted = Tracker(self.issues, self.mail, Path(self.tmp.name), lambda: self.now)
        second = restarted.create("request-1", "查核 084", "查出差異來源", "唯讀", "提出有來源的結論", "codex task", [])
        self.assertEqual(first["issue"], second["issue"])
        self.assertEqual(len(self.issues.list()), 1)
        self.assertEqual(second["status"], "待派工")

    def test_dispatch_is_not_repeated_when_retried(self):
        task = self.create()
        args = (task['issue'], 'alpha', 'request-1-a', '唯讀調查差異', 1600)
        self.tracker.dispatch(*args)
        self.tracker.dispatch(*args)
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.mail.woken, ['alpha'])
        self.assertEqual(self.tracker.read(task['issue'])['status'], '執行中')

    def assigned(self):
        task = self.create()
        self.tracker.dispatch(task['issue'], 'alpha', 'request-1-a', '唯讀調查差異', 1600)
        return task['issue']

    def report(self, message_id=51):
        self.mail.messages.append(dict(id=message_id, sender='alpha', thread_id='request-1-a', body_md='已完成，證據在原票'))

    def test_worker_report_waits_for_review_then_is_saved_before_ack(self):
        number = self.assigned()
        self.report()
        self.tracker.poll()
        self.assertEqual(self.tracker.read(number)['status'], '待 Orch 核對')
        self.assertEqual(self.mail.acked, [])
        self.tracker.review(number, 51, '已完成', '已讀原票並核對來源版本', '核對整體完成條件')
        self.assertEqual(self.mail.acked, [51])
        self.assertEqual(self.tracker.read(number)['reports']['51']['review']['evidence'], '已讀原票並核對來源版本')
        self.assertNotEqual(self.tracker.read(number)['status'], '已完成')

    def test_github_failure_keeps_mail_until_restart_repairs_projection(self):
        number = self.assigned()
        self.report()
        self.tracker.poll()
        self.issues.fail_projection = True
        with self.assertRaises(OSError):
            self.tracker.review(number, 51, '已完成', '核實版本', '整體核對')
        self.assertEqual(self.mail.acked, [])
        self.issues.fail_projection = False
        restarted = Tracker(self.issues, self.mail, Path(self.tmp.name), lambda: self.now)
        restarted.poll()
        self.assertEqual(self.mail.acked, [51])
        self.assertIn('review:51', restarted.read(number)['events'])
        count = len(self.issues.items[number]['comments'])
        restarted.poll()
        self.assertEqual(len(self.issues.items[number]['comments']), count)

    def test_timeout_pings_once_and_notifies_only_after_twenty_minutes(self):
        number = self.assigned()
        self.now = 1599
        self.assertEqual(self.tracker.poll()['notifications'], [])
        self.assertEqual(len(self.mail.sent), 1)
        self.now = 1600
        self.tracker.poll()
        self.assertEqual(len(self.mail.sent), 2)
        self.now = 2799
        self.assertEqual(self.tracker.poll()['notifications'], [])
        self.now = 2800
        result = self.tracker.poll()
        self.assertEqual(len(result['notifications']), 1)
        self.assertEqual(self.tracker.read(number)['status'], '回報逾時')
        self.assertEqual(len(self.mail.sent), 2)
        event = result['notifications'][0]
        claim = self.tracker.claim_notification(number, event['key'])
        self.tracker.confirm_notification(number, event['key'], 'Codex notification receipt', claim['claim_token'])
        self.assertEqual(self.tracker.poll()['notifications'], [])

    def test_completion_requires_review_and_stops_timeout_checks(self):
        number = self.assigned()
        with self.assertRaises(ValueError):
            self.tracker.finish(number, '已完成', '只是 worker 開始執行')
        self.report()
        self.tracker.poll()
        self.tracker.review(number, 51, '已完成', '核對原票結果', '整體完成條件已滿足')
        self.tracker.finish(number, '已完成', '已核對調查結論及來源，符合原要求')
        self.now = 10000
        result = self.tracker.poll()
        self.assertEqual(self.issues.items[number]['state'], 'closed')
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(len(result['notifications']), 1)

    def test_late_report_suppresses_unsent_timeout_notification(self):
        number = self.assigned()
        self.now = 1600
        self.tracker.poll()
        self.now = 2800
        self.assertEqual(len(self.tracker.poll()['notifications']), 1)
        self.report()
        self.assertEqual(self.tracker.poll()['notifications'], [])
        self.assertEqual(self.tracker.read(number)['status'], '待 Orch 核對')

    def test_in_scope_followup_uses_same_assignment_without_duplicate_send(self):
        number = self.assigned()
        self.report()
        self.tracker.poll()
        self.tracker.review(number, 51, '執行中', '缺少同次同步資料', '補查原範圍證據', 2000)
        self.tracker.instruct(number, 'request-1-a', 'followup-1', '請補查同次同步來源', 2000)
        self.tracker.instruct(number, 'request-1-a', 'followup-1', '請補查同次同步來源', 2000)
        self.assertEqual(len(self.mail.sent), 2)
        self.assertEqual(self.mail.sent[-1][1], 'request-1-a')
        self.assertEqual(self.tracker.read(number)['assignments']['request-1-a']['due'], 2000)

    def test_unknown_report_is_not_acknowledged_or_attached(self):
        number = self.assigned()
        self.mail.messages.append(dict(id=92, sender='another-project', thread_id='request-1-a', body_md='完成'))
        result = self.tracker.poll()
        self.assertEqual(result['unknown_message_ids'], [92])
        self.assertEqual(self.tracker.read(number)['reports'], {})
        self.assertEqual(self.mail.acked, [])

    def test_concurrent_dispatch_and_uncertain_delivery_do_not_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        number = self.create()['issue']
        def dispatch():
            return self.tracker.dispatch(number, 'alpha', 'concurrent', '調查', 1600)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: dispatch(), range(2)))
        self.assertEqual(len(self.mail.sent), 1)
        def interrupted(*args):
            self.mail.sent.append(args)
            raise OSError('Reply lost after sending')
        self.mail.send = interrupted
        with self.assertRaises(OSError):
            self.tracker.dispatch(number, 'alpha', 'uncertain', '查另一來源', 1600)
        self.tracker.dispatch(number, 'alpha', 'uncertain', '查另一來源', 1600)
        self.assertEqual(len(self.mail.sent), 2)
        self.assertEqual(self.tracker.read(number)['assignments']['uncertain']['status'], '送達待確認')

    def test_one_worker_progress_does_not_hide_another_needing_nat(self):
        number = self.assigned()
        self.tracker.dispatch(number, 'beta', 'request-1-b', '查核另一來源', 1600)
        self.report()
        self.mail.messages.append(dict(id=52, sender='beta', thread_id='request-1-b', body_md='持續處理'))
        self.tracker.poll()
        self.tracker.review(number, 51, '待 Nat 決策', '需 Nat 選定來源', '請 Nat 決定')
        self.tracker.review(number, 52, '執行中', '已確認來源可讀', '繼續原範圍', 2000)
        self.assertEqual(self.tracker.read(number)['status'], '待 Nat 決策')

    def test_explicit_stop_keeps_issue_but_stops_pings_and_notifications(self):
        number = self.assigned()
        self.tracker.stop(number, 'Nat 要求暫停追蹤')
        self.now = 10000
        self.assertEqual(self.tracker.poll()['notifications'], [])
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.issues.items[number]['state'], 'open')

    def test_manual_issue_close_stops_tracking_without_reopening(self):
        number = self.assigned()
        self.issues.items[number]['state'] = 'closed'
        self.now = 10000
        self.tracker.poll()
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.issues.items[number]['state'], 'closed')

    def test_uncertain_send_is_actionable_and_can_be_reconciled_without_resend(self):
        number = self.create()['issue']
        def lost(*args):
            self.mail.sent.append(args)
            raise OSError('Lost receipt')
        self.mail.send = lost
        with self.assertRaises(OSError):
            self.tracker.dispatch(number, 'alpha', 'lost', '查核', 1600)
        result = self.tracker.poll()
        self.assertEqual(len(result['notifications']), 1)
        self.tracker.reconcile_delivery(number, 'lost', {'message_id': 88}, '已在信箱核對訊息 88 的收件 project 與 task_id')
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.mail.woken, ['alpha'])
        self.assertEqual(self.tracker.poll()['notifications'], [])

    def test_restart_repairs_issue_closed_after_completion_comment_was_saved(self):
        number = self.assigned()
        self.report()
        self.tracker.poll()
        self.tracker.review(number, 51, '已完成', '核對證據', '結案')
        self.issues.fail_projection = True
        with self.assertRaises(OSError):
            self.tracker.finish(number, '已完成', '符合所有完成條件')
        self.assertEqual(self.issues.items[number]['state'], 'open')
        self.issues.fail_projection = False
        self.tracker.poll()
        self.assertEqual(self.issues.items[number]['state'], 'closed')

    def test_resolved_blocker_is_not_delivered_as_a_stale_notification(self):
        number = self.assigned()
        self.report()
        self.tracker.poll()
        self.tracker.review(number, 51, '待 Nat 決策', '缺來源', 'Nat 指定來源')
        self.assertEqual(len(self.tracker.poll()['notifications']), 1)
        self.tracker.instruct(number, 'request-1-a', 'nat-decision-1', 'Operator 已決定來源，繼續原工作', 2000)
        self.assertEqual(self.tracker.poll()['notifications'], [])

    def test_review_of_older_report_cannot_complete_newer_running_work(self):
        number = self.assigned()
        self.report(51)
        self.report(52)
        self.tracker.poll()
        self.tracker.review(number, 52, '執行中', '較新回報仍在處理', '繼續核對', 2500)
        self.tracker.review(number, 51, '已完成', '較舊回報當時完成', '僅保留歷史證據')
        assignment = self.tracker.read(number)['assignments']['request-1-a']
        self.assertEqual(assignment['status'], '執行中')
        self.assertEqual(assignment['due'], 2500)
        with self.assertRaises(ValueError):
            self.tracker.finish(number, '已完成', '舊證據不夠')

    def test_same_delivered_blocker_is_not_notified_again_for_a_new_report(self):
        number = self.assigned()
        self.report(51)
        self.tracker.poll()
        self.tracker.review(number, 51, '待 Nat 決策', '第一次查核證據', 'Nat 選定資料來源')
        event = self.tracker.poll()['notifications'][0]
        claim = self.tracker.claim_notification(number, event['key'])
        self.tracker.confirm_notification(number, event['key'], 'native-delivery-1', claim['claim_token'])
        self.report(52)
        self.tracker.poll()
        self.tracker.review(number, 52, '待 Nat 決策', '不同時間再次查核仍相同', 'Nat 選定資料來源')
        self.assertEqual(self.tracker.poll()['notifications'], [])

    def test_notification_claim_prevents_resend_after_crash_or_second_sender(self):
        number = self.assigned()
        self.report()
        self.tracker.poll()
        self.tracker.review(number, 51, '待 Nat 決策', '核對證據', 'Nat 選來源')
        event = self.tracker.poll()['notifications'][0]
        claim = self.tracker.claim_notification(number, event['key'])
        self.assertTrue(claim['claimed'])
        self.assertFalse(self.tracker.claim_notification(number, event['key'])['claimed'])
        restarted = Tracker(self.issues, self.mail, Path(self.tmp.name), lambda: self.now)
        result = restarted.poll()
        self.assertEqual(result['notifications'], [])
        self.assertEqual(len(result['uncertain_notifications']), 1)
        with self.assertRaises(ValueError):
            restarted.confirm_notification(number, event['key'], 'native-receipt', 'wrong-token')
        restarted.confirm_notification(number, event['key'], 'native-receipt', claim['claim_token'])
        self.assertEqual(restarted.poll()['uncertain_notifications'], [])

    def test_late_old_report_does_not_suppress_timeout_of_newer_work(self):
        number = self.assigned()
        self.report(52)
        self.tracker.poll()
        self.tracker.review(number, 52, '執行中', '新的工作狀態', '繼續', 1600)
        self.now = 1600
        self.tracker.poll()
        self.now = 2800
        self.assertEqual(len(self.tracker.poll()['notifications']), 1)
        self.report(51)
        self.assertEqual(len(self.tracker.poll()['notifications']), 1)



if __name__ == "__main__":
    unittest.main()
