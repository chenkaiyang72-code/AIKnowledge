from __future__ import annotations

import unittest

from aikb.structured_chunks import build_chunks


class StructuredChunkTests(unittest.TestCase):
    def test_c_parser_extracts_functions_macros_and_types(self) -> None:
        source = (
            b"/** demo docs */\n"
            b"static int demo_init(void) { return 0; }\n"
            b"#define DEMO_VALUE(x) ((x) + 1)\n"
            b"typedef struct demo { int value; } demo_t;\n"
            b"#ifdef CONFIG_DEMO\n"
            b"void demo_exit(void) { nested_call(); }\n"
            b"#endif\n"
        )
        outcome = build_chunks(source, "c", chunk_lines=120, overlap=20)

        self.assertEqual(outcome.parse_status, "structured")
        self.assertEqual(outcome.syntax_error_count, 0)
        by_symbol = {chunk.symbol: chunk for chunk in outcome.chunks}
        self.assertEqual(by_symbol["demo_init"].kind, "function")
        self.assertEqual(by_symbol["DEMO_VALUE"].kind, "macro")
        self.assertEqual(by_symbol["demo_t"].kind, "type")
        self.assertEqual(by_symbol["demo_exit"].kind, "function")
        self.assertTrue(by_symbol["demo_init"].content.startswith("/** demo docs */"))

    def test_non_c_file_uses_line_window_fallback(self) -> None:
        source = "one\ntwo\nthree\nfour\n".encode()
        outcome = build_chunks(source, "text", chunk_lines=3, overlap=1)

        self.assertEqual(outcome.parse_status, "not_applicable")
        self.assertEqual(len(outcome.chunks), 2)
        self.assertEqual(outcome.chunks[0].generator, "line-window-v1")
        self.assertEqual(
            (outcome.chunks[1].start_line, outcome.chunks[1].end_line), (3, 4)
        )

    def test_deep_preprocessor_tree_does_not_use_python_recursion(self) -> None:
        depth = 1_100
        source = (
            ("#if CONFIG_DEEP\n" * depth)
            + "static int deep_marker(void) { return 1; }\n"
            + ("#endif\n" * depth)
        ).encode("utf-8")

        outcome = build_chunks(source, "c", chunk_lines=80, overlap=10)

        self.assertTrue(
            any(chunk.symbol == "deep_marker" for chunk in outcome.chunks)
        )


if __name__ == "__main__":
    unittest.main()
