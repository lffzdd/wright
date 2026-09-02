"""Protocol 协议解析与修调防御测试。"""

import unittest

from wright.protocol import TurnAbort, parse_turn, _loads


class TestProtocolParsing(unittest.TestCase):
    def test_standard_valid_final_turn(self):
        raw = '{"reasoning": "all good", "tool_calls": [], "final_answer": "Task completed!"}'
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "final")
        self.assertEqual(turn.final_answer, "Task completed!")

    def test_standard_valid_tool_calls_turn(self):
        raw = '{"reasoning": "need plan", "tool_calls": [{"name": "update_plan", "arguments": {"step_id": "s1"}}], "final_answer": null}'
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "tool_calls")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "update_plan")

    def test_markdown_wrapper_stripping(self):
        raw = """```json
{
  "reasoning": "wrapped in markdown",
  "tool_calls": [],
  "final_answer": "Done"
}
```"""
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "final")
        self.assertEqual(turn.final_answer, "Done")

    def test_raw_unescaped_newlines_in_json_string(self):
        raw = """{
  "reasoning": "验证完成",
  "tool_calls": [],
  "final_answer": "马里奥小游戏做好了！🎮

**文件位置**：index.html
- 第一项
- 第二项"
}"""
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "final")
        self.assertIn("马里奥小游戏做好了！🎮\n\n**文件位置**：index.html\n- 第一项\n- 第二项", turn.final_answer)

    def test_trailing_commas_repair(self):
        raw = """{
  "reasoning": "trailing comma test",
  "tool_calls": [
    {
      "name": "update_plan",
      "arguments": {
        "step_id": "step_1",
      },
    },
  ],
  "final_answer": null,
}"""
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "tool_calls")
        self.assertEqual(turn.tool_calls[0].name, "update_plan")

    def test_extra_commentary_wrapping(self):
        raw = """Here is the response:
{
  "reasoning": "extra text around json",
  "tool_calls": [],
  "final_answer": "Result"
}
Hope this helps!"""
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "final")
        self.assertEqual(turn.final_answer, "Result")

    def test_commentary_suffix_with_brace_does_not_break_extraction(self):
        raw = """<think>compare {draft} first</think>
{"reasoning":"ok","tool_calls":[],"final_answer":"keep {x}"}
Explanation ends with }"""
        turn = parse_turn(raw)
        self.assertEqual(turn.final_answer, "keep {x}")

    def test_trailing_comma_repair_does_not_change_string_content(self):
        raw = """{
  "reasoning": "ok",
  "tool_calls": [],
  "final_answer": "keep ,} and ,] exactly",
}"""
        turn = parse_turn(raw)
        self.assertEqual(turn.final_answer, "keep ,} and ,] exactly")

    def test_missing_outer_closing_brace_is_repaired(self):
        raw = (
            '{"reasoning":"done","tool_calls":[],"final_answer":'
            '"line 1\n\nline 2 😄"'
        )
        turn = parse_turn(raw)
        self.assertEqual(turn.final_answer, "line 1\n\nline 2 😄")

    def test_missing_nested_closing_brackets_are_repaired(self):
        raw = (
            '{"reasoning":"call","tool_calls":['
            '{"name":"update_plan","arguments":{}}'
        )
        turn = parse_turn(raw)
        self.assertEqual(turn.kind, "tool_calls")
        self.assertEqual(turn.tool_calls[0].name, "update_plan")

    def test_unclosed_string_is_not_guessed(self):
        with self.assertRaises(TurnAbort):
            _loads('{"reasoning":"done","tool_calls":[],"final_answer":"cut off')

    def test_all_unescaped_control_characters_are_repaired(self):
        raw = (
            '{"reasoning":"ok","tool_calls":[],"final_answer":"a'
            + "\x00\x08\x0c"
            + 'b"}'
        )
        turn = parse_turn(raw)
        self.assertEqual(turn.final_answer, "a\x00\x08\x0cb")

    def test_utf8_bom_is_accepted(self):
        raw = '\ufeff{"reasoning":"ok","tool_calls":[],"final_answer":"done"}'
        self.assertEqual(parse_turn(raw).final_answer, "done")

    def test_double_encoded_object_is_accepted(self):
        raw = '"{\\"reasoning\\":\\"ok\\",\\"tool_calls\\":[],\\"final_answer\\":\\"done\\"}"'
        self.assertEqual(parse_turn(raw).final_answer, "done")

    def test_decode_error_contains_location_and_context(self):
        with self.assertRaises(TurnAbort) as ctx:
            _loads('{"reasoning": nope}')
        message = str(ctx.exception)
        self.assertIn("line 1", message)
        self.assertIn("nearby text", message)

    def test_exactly_one_rule_violation_raises_turn_abort(self):
        raw = """{
  "reasoning": "both set",
  "tool_calls": [{"name": "update_plan", "arguments": {}}],
  "final_answer": "Both set"
}"""
        with self.assertRaises(TurnAbort) as ctx:
            parse_turn(raw)
        self.assertIn("exactly one", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
