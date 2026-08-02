from __future__ import annotations

import unittest

from aikb.analysis_artifacts import (
    decode_analysis_artifact,
    encode_analysis_artifact,
)
from aikb.source_relations import extract_source_facts
from aikb.structured_chunks import build_chunks


class AnalysisArtifactTests(unittest.TestCase):
    def test_round_trip_preserves_chunks_conditions_and_relations(self) -> None:
        source = (
            b"#ifdef CONFIG_DEMO\n"
            b"static int helper(void) { return 1; }\n"
            b"int demo(void) { return helper(); }\n"
            b"#endif\n"
        )
        parse_outcome = build_chunks(source, "c", chunk_lines=120, overlap=20)
        source_facts = extract_source_facts(source, "c")

        payload = encode_analysis_artifact(parse_outcome, source_facts)
        decoded_parse, decoded_facts = decode_analysis_artifact(payload)

        self.assertEqual(decoded_parse, parse_outcome)
        self.assertEqual(decoded_facts, source_facts)


if __name__ == "__main__":
    unittest.main()
