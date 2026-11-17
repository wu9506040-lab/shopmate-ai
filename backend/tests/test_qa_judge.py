"""质检 LLM 层单测 —— 不依赖真模型，用假 provider 锁协议与校准逻辑。"""
import json
import unittest

from app.services.qa import judge


class FakeProvider:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def chat(self, messages, model=None, temperature=0.7, max_tokens=None,
             tools=None, tool_choice=None):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens})
        return {"reply": self.reply, "usage": {}}


def _json(dims, failure="none", reason="ok"):
    d = dict(zip(judge.DIMS, dims))
    d.update({"failure": failure, "reason": reason})
    return json.dumps(d, ensure_ascii=False)


class QaJudgeTest(unittest.TestCase):
    def test_parse_plain_json(self):
        d = judge._parse(_json([4, 3, 5, 2], "policy", "政策说错"))
        self.assertEqual(d["relevance"], 4)
        self.assertEqual(d["actionable"], 2)
        self.assertEqual(d["failure"], "policy")

    def test_parse_code_fence_and_noise(self):
        raw = "这是打分结果：\n```json\n" + _json([5, 5, 4, 4]) + "\n```"
        d = judge._parse(raw)
        self.assertIsNotNone(d)
        self.assertEqual(judge.score_of(d), 4.5)

    def test_parse_clamps_out_of_range(self):
        d = judge._parse(_json([9, 0, 3, 3]))
        self.assertEqual(d["relevance"], 5)
        self.assertEqual(d["grounding"], 1)

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(judge._parse("我觉得还不错"))
        self.assertIsNone(judge._parse(_json([4, 4, 4, 4]).replace("actionable", "x")))

    def test_judge_one_uses_zero_temperature(self):
        p = FakeProvider(_json([4, 4, 4, 4]))
        d = judge.judge_one(p, "这个能退吗", "7 天内可以[1]。", "退换货政策")
        self.assertIsNotNone(d)
        self.assertEqual(p.calls[0]["temperature"], 0.0, "打分必须温度 0，否则同一答案两次分数不同")
        self.assertEqual(p.calls[0]["max_tokens"], 220)

    def test_worst_dim_drives_verdict(self):
        good = judge._parse(_json([5, 5, 5, 5]))
        bad_grounding = judge._parse(_json([5, 1, 5, 5]))
        self.assertEqual(judge.worst_dim(good), 5)
        self.assertEqual(judge.worst_dim(bad_grounding), 1,
                         "均值会把「答得漂亮但依据是编的」拉到及格，判定必须看最低维")

    def test_calibration_passes_when_bad_samples_get_low_scores(self):
        p = FakeProvider(_json([2, 2, 3, 2]))
        ok, avg = judge.calibrate(p)
        self.assertTrue(ok)
        self.assertLess(avg, 3.0)

    def test_calibration_fails_when_bad_samples_get_high_scores(self):
        p = FakeProvider(_json([5, 4, 5, 4]))
        ok, avg = judge.calibrate(p)
        self.assertFalse(ok, "坏答案拿高分说明这一批打分不可信")
        self.assertGreaterEqual(avg, 3.0)

    def test_calibration_fails_on_parse_error(self):
        ok, _ = judge.calibrate(FakeProvider("无法打分"))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
