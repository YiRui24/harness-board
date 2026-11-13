"""反问卡服务端契约，全部使用假请求，不调用设备或发送适配器。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'board'))
import normalize as N
import server as S


def sample():
    return {'id': 'sample', 'kind': 'question', 'card': {'questions': [
        {'header': '第一题', 'question': '选择', 'required': True,
         'options': [{'label': '甲', 'description': '说明'}, {'label': '乙'}]},
        {'question': '多选', 'required': False, 'multiSelect': True,
         'options': [{'label': '一'}, {'label': '二'}]},
        {'question': '文本', 'required': True, 'inputType': 'text'},
        {'question': '其他', 'required': False, 'isOther': True, 'options': [{'label': '已有'}]},
    ]}}


class QuestionTests(unittest.TestCase):
    def setUp(self):
        S._question_sends.clear()
        self.raw = sample()
        self.records = []
        def fake_send(body):
            rec = {'id': str(len(self.records)), 'phase': 'sending', 'text': body['message']}
            self.records.append(rec)
            return rec
        self.patches = [patch.object(S, 'items_local', side_effect=lambda *a, **k: ([self.raw], None, None)),
                        patch.object(S, 'list_queue_local', return_value=[]),
                        patch.object(S, 'question_message_capability', return_value=True),
                        patch.object(S, 'start_send', side_effect=fake_send)]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.body = {'app': 'codex', 'id': 'fake-session', 'itemId': 'sample', 'action': 'answer',
                     'requestVersion': S.question_version(N.normalize_item('codex', self.raw)[0]['card']['questions']),
                     'answers': [{'selected': [1], 'text': ''}, {'selected': [1], 'text': ''},
                                 {'selected': [], 'text': '示例文本'}, {'selected': [], 'text': '自填'}]}

    def test_all_questions_and_deduplication(self):
        result = S.handle_act(self.body)
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]['text'], '第一题：选择\n乙\n\n多选\n二\n\n文本\n示例文本\n\n其他\n自填')
        self.assertTrue(S.handle_act(self.body)['ok'])
        self.assertEqual(len(self.records), 1)
        self.records[0]['phase'] = 'failed'
        self.assertTrue(S.handle_act(self.body)['ok'])
        self.assertEqual(len(self.records), 2)

    def test_required_optional_and_other_validation(self):
        self.body['answers'][0]['selected'] = []
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.assertEqual(self.records, [])
        self.body['answers'][0]['selected'] = [0]
        self.body['answers'][1]['selected'] = []
        self.body['answers'][3]['text'] = ''
        self.assertTrue(S.handle_act(self.body)['ok'])
        self.assertIn('多选\n（跳过）', self.records[0]['text'])
        S._question_sends.clear()
        self.body['answers'][0]['text'] = '禁止自填'
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.assertEqual(len(self.records), 1)

    def test_native_unknown_secret_and_done_do_not_send(self):
        with patch.object(S, 'question_message_capability', return_value=False):
            self.assertFalse(S.handle_act(self.body)['ok'])
        self.raw['card']['questions'][0]['isSecret'] = True
        item = N.normalize_item('codex', self.raw)[0]
        self.assertEqual(item['card']['questions'], [])
        self.assertNotIn('选择', str(item))
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.raw = sample()
        self.raw['card']['answered'] = True
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.raw = sample()
        self.raw['card']['fallback'] = 'done'
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.raw = sample()
        self.raw['card']['questions'][0] = {'question': '未知题型', 'required': True}
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.raw = sample()
        del self.raw['card']['questions'][0]['required']
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.assertEqual(self.records, [])

    def test_reused_id_with_new_questions_is_new_request(self):
        self.assertTrue(S.handle_act(self.body)['ok'])
        self.raw['card']['questions'][0]['question'] = '新的题'
        self.assertFalse(S.handle_act(self.body)['ok'])
        self.body['requestVersion'] = S.question_version(N.normalize_item('codex', self.raw)[0]['card']['questions'])
        self.assertTrue(S.handle_act(self.body)['ok'])
        self.assertEqual(len(self.records), 2)

if __name__ == '__main__':
    unittest.main()
