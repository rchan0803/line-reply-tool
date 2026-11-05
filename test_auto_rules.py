"""自動返信の判定ルールのテスト。python test_auto_rules.py で実行する。"""
import unittest

import auto_rules as r

TEMPLATE = "メッセージありがとうございます\nまた、本当に嬉しいです。\n\n〇〇様のことが\n\nコース案内\n\nお支払い後はお送りください\n\nP.S."
FREE = "キタコ様\n\n鑑定です\n【本鑑定で視えること🕊️】\n・原因"
CONV = {"line_name": "キタコ (kitajo)"}


def user(text, i=10, t="text"):
    return {"message_id": i, "sender_type": "user", "message_type": t, "content": text, "sent_at": f"2026-11-05 10:00:{i:02d}"}


def bot(text, i=5, source=None):
    return {"message_id": i, "sender_type": "bot", "message_type": "text", "content": text, "source": source, "sent_at": f"2026-11-05 09:00:{i:02d}"}


def order(status="支払済み", valid=True, course="スタンダード", name="田中 花子", no="1234567890"):
    return {"注文番号": no, "氏名": name, "ステータス": status, "有効": valid, "コース": course, "アップセル": False}


GOOD = {
    "category": "honkantei_request", "has_other_question": False, "money_reason": False, "needs_care": False,
    "opening": "お声をいただき、ありがとうございます。",
    "recommendation": "キタコ様の場合は、根本原因をじっくり視たいので、スタンダードコースをおすすめいたします。",
    "reason": "",
}


class Waiting(unittest.TestCase):
    def test_replied_already(self):
        self.assertFalse(r.waiting_for_reply([user("本鑑定希望", 1), bot("お返事", 2)]))

    def test_scenario_is_not_reply(self):
        msgs = [user("こんにちは", 1), bot("自動", 2, {"type": "scenario", "name": "x"})]
        self.assertTrue(r.waiting_for_reply(msgs))

    def test_block_after_last_reply(self):
        msgs = [user("古い", 1), bot("返信", 2), user("a", 3), user("b", 4)]
        self.assertEqual(r.block_text(r.pending_block(msgs)), "a\nb")

    def test_flow_only(self):
        self.assertTrue(r.is_flow_only([user("【受け取る】")]))
        self.assertTrue(r.is_flow_only([user("【理想の私】\n笑顔")]))
        self.assertFalse(r.is_flow_only([user("本鑑定希望")]))


class NightGreeting(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(r.add_night_greeting("承知いたしました。"), "夜遅くに失礼いたします。" + "\n" + "承知いたしました。")

    def test_after_name_line(self):
        self.assertEqual(
            r.add_night_greeting("優子様" + "\n" + "このたびは"),
            "\n".join(["優子様", "夜遅くに失礼いたします。", "このたびは"]),
        )


class Guard(unittest.TestCase):
    def test_stop_word(self):
        self.assertEqual(r.check_block([user("返金してください")]).action, "review")

    def test_image(self):
        self.assertEqual(r.check_block([user("x", t="image")]).action, "review")

    def test_ok(self):
        self.assertIsNone(r.check_block([user("本鑑定希望")]))


class Honkantei(unittest.TestCase):
    msgs = [bot(FREE), user("本鑑定希望")]

    def test_sends_template_verbatim(self):
        d = r.decide_honkantei("キタコ", GOOD, [], self.msgs, TEMPLATE)
        self.assertEqual(d.action, "send")
        self.assertEqual(d.status_id, r.STATUS_PROSPECT)
        self.assertIn("本当に嬉しいです。\n\nお声をいただき", d.messages[0])
        self.assertIn("おすすめいたします。\n\nお支払い後は", d.messages[0])
        self.assertNotIn("〇〇", d.messages[0])

    def test_blocks(self):
        cases = [
            ("", GOOD, [], self.msgs),
            ("キタコ", GOOD, [], [user("本鑑定希望")]),
            ("キタコ", GOOD, [order()], self.msgs),
            ("キタコ", {**GOOD, "has_other_question": True}, [], self.msgs),
            ("キタコ", {**GOOD, "needs_care": True}, [], self.msgs),
            ("キタコ", {**GOOD, "opening": "「安心」が響きました。ありがとうございます"}, [], self.msgs),
            ("キタコ", {**GOOD, "recommendation": "ライトがおすすめです。"}, [], self.msgs),
            ("キタコ", GOOD, [], [bot(FREE), bot(r.STANDARD_URL_KEY, 6), user("本鑑定希望")]),
        ]
        for name, res, orders, msgs in cases:
            self.assertEqual(r.decide_honkantei(name, res, orders, msgs, TEMPLATE).action, "review", res)

    def test_template_changed(self):
        self.assertEqual(r.decide_honkantei("キタコ", GOOD, [], self.msgs, "別の文").action, "review")


class Decline(unittest.TestCase):
    msgs = [bot(FREE), user("今回は受けません")]
    plain = {"money_reason": False, "has_other_question": False, "needs_care": False}

    def test_sends(self):
        d = r.decide_decline(self.plain, "今回は受けません", [], self.msgs, "承知いたしました。")
        self.assertEqual((d.action, d.status_id), ("send", 0))

    def test_money_reason_goes_to_human(self):
        d = r.decide_decline({**self.plain, "money_reason": True}, "お金がなくて", [], self.msgs, "x")
        self.assertEqual(d.action, "review")

    def test_long_goes_to_human(self):
        self.assertEqual(r.decide_decline(self.plain, "あ" * 200, [], self.msgs, "x").action, "review")


class Purchase(unittest.TestCase):
    def run_it(self, text, orders, msgs=(), other=False, name="花子"):
        return r.decide_purchase(
            name, {"line_name": "はな (hana)"}, text, orders, list(msgs), lambda o: other,
            f"〇〇様が一歩踏み出した証です {r.VIP_URL}",
        )

    def test_sends_with_order_number(self):
        d = self.run_it("1234567890 購入しました", [order()])
        self.assertEqual((d.action, d.status_id), ("send", r.STATUS_HANDLING))
        self.assertIn(r.VIP_URL, d.messages[0])
        self.assertEqual(d.buyer["customer_name"], "田中 花子")

    def test_sends_with_full_name(self):
        self.assertEqual(self.run_it("購入完了\n田中花子", [order()]).action, "send")

    def test_unpaid_goes_to_human(self):
        self.assertEqual(self.run_it("購入完了 田中花子", [order("未払い", False)]).action, "review")

    def test_no_match(self):
        self.assertEqual(self.run_it("購入完了", [order()]).action, "review")

    def test_two_orders(self):
        self.assertEqual(self.run_it("購入完了 田中花子", [order(), order(no="9999999999")]).action, "review")

    def test_repeater(self):
        self.assertEqual(self.run_it("購入完了 田中花子", [order()], other=True).action, "review")

    def test_already_invited(self):
        self.assertEqual(self.run_it("購入完了 田中花子", [order()], [bot(r.VIP_URL)]).action, "review")

    def test_upsell_not_matched(self):
        self.assertEqual(self.run_it("購入完了 田中花子", [{**order(), "アップセル": True}]).action, "review")

    def test_given_name_fallback(self):
        d = self.run_it("購入完了 田中花子", [order()], name="")
        self.assertIn("花子様", d.messages[0])


class Hearing(unittest.TestCase):
    T = {"ライト": ("〇〇様\nライト1", "記入L"), "スタンダード": ("〇〇様\n標準1", "記入S")}
    row = {"コース": "スタンダードコース", "名": "優子", "有効": True, "鑑定書送付済み": False, "リピーター": False}

    def test_sends_two(self):
        d = r.decide_hearing([self.row], [user("よろしくお願いします")], self.T)
        self.assertEqual(d.messages, ["優子様\n標準1", "記入S"])

    def test_light(self):
        d = r.decide_hearing([{**self.row, "コース": "ライトコース"}], [], self.T)
        self.assertEqual(d.messages[1], "記入L")

    def test_blocks(self):
        for rows in ([], [self.row, self.row], [{**self.row, "有効": False}],
                     [{**self.row, "リピーター": True}], [{**self.row, "名": ""}],
                     [{**self.row, "コース": ""}]):
            self.assertEqual(r.decide_hearing(rows, [], self.T).action, "review", rows)

    def test_long_first_message(self):
        self.assertEqual(r.decide_hearing([self.row], [user("あ" * 100)], self.T).action, "review")


if __name__ == "__main__":
    unittest.main()
