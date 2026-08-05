from __future__ import annotations

import unittest

from aikb.source_relations import (
    AMBIGUOUS_CANDIDATE,
    SOURCE_EXACT,
    SOURCE_INFERRED,
    build_path_basename_index,
    extract_source_facts,
    include_candidates,
)


class SourceRelationTests(unittest.TestCase):
    def test_c_facts_preserve_conditions_and_call_candidates(self) -> None:
        source = (
            b'#include "demo.h"\n'
            b"#ifdef CONFIG_DEMO\n"
            b"static int helper(void) { return 1; }\n"
            b"int demo(void) { return helper(); }\n"
            b"#else\n"
            b"int demo(void) { return ops->fallback(); }\n"
            b"#endif\n"
        )

        facts = extract_source_facts(source, "c")

        definitions = {
            (item.name, item.role, item.namespace_scope): item
            for item in facts.occurrences
        }
        self.assertIn(("helper", "definition", "file"), definitions)
        self.assertIn(("demo", "definition", "repository"), definitions)
        helper = definitions[("helper", "definition", "file")]
        self.assertIn("defined(CONFIG_DEMO)", helper.condition.expression)

        includes = [item for item in facts.relations if item.kind == "includes"]
        self.assertEqual(includes[0].target_text, "demo.h")
        self.assertEqual(includes[0].confidence, SOURCE_EXACT)
        calls = [item for item in facts.relations if item.kind == "calls"]
        by_target = {item.target_text: item for item in calls}
        self.assertEqual(by_target["helper"].confidence, SOURCE_INFERRED)
        self.assertEqual(by_target["fallback"].confidence, AMBIGUOUS_CANDIDATE)
        self.assertIn("!(", by_target["fallback"].condition.expression)

    def test_kconfig_facts_extract_dependencies_and_selects(self) -> None:
        source = (
            "config DEMO\n"
            "    depends on NET && X86\n"
            "    select HELPER if EXPERT\n"
        ).encode()

        facts = extract_source_facts(source, "kconfig")

        self.assertEqual(facts.occurrences[0].name, "DEMO")
        relations = {(item.kind, item.target_text): item for item in facts.relations}
        self.assertIn(("depends_on_config", "NET"), relations)
        self.assertIn(("depends_on_config", "X86"), relations)
        selected = relations[("selects_config", "HELPER")]
        self.assertEqual(selected.condition.expression, "EXPERT")

    def test_kbuild_facts_keep_config_as_condition(self) -> None:
        facts = extract_source_facts(
            b"obj-$(CONFIG_DEMO) += demo.o helper.o\n", "kbuild"
        )

        self.assertEqual(len(facts.relations), 2)
        self.assertEqual(
            {item.target_text for item in facts.relations}, {"demo.o", "helper.o"}
        )
        self.assertTrue(
            all(item.condition.expression == "CONFIG_DEMO" for item in facts.relations)
        )

    def test_include_candidates_normalize_parent_segments(self) -> None:
        available_paths = {"include/demo.h", "arch/x86/include/demo.h"}
        candidates = include_candidates(
            "kernel/demo.c",
            "../include/demo.h",
            available_paths,
            build_path_basename_index(available_paths),
        )
        self.assertEqual(candidates, ["include/demo.h"])

    def test_include_candidates_use_basename_index(self) -> None:
        available_paths = {
            "drivers/net/foo.c",
            "include/linux/foo.h",
            "arch/x86/include/linux/foo.h",
        }
        candidates = include_candidates(
            "drivers/net/foo.c",
            "linux/foo.h",
            available_paths,
            build_path_basename_index(available_paths),
        )
        self.assertEqual(
            candidates,
            ["arch/x86/include/linux/foo.h", "include/linux/foo.h"],
        )

    def test_deep_preprocessor_tree_extracts_facts_iteratively(self) -> None:
        depth = 1_100
        source = (
            ("#if CONFIG_DEEP\n" * depth)
            + "static int deep_relation_marker(void) { return 1; }\n"
            + ("#endif\n" * depth)
        ).encode("utf-8")

        facts = extract_source_facts(source, "c")

        self.assertTrue(
            any(item.name == "deep_relation_marker" for item in facts.occurrences)
        )


if __name__ == "__main__":
    unittest.main()
