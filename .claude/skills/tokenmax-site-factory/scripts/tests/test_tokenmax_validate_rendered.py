from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tokenmax_validate_rendered.py"
SPEC = importlib.util.spec_from_file_location("tokenmax_validate_rendered", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RenderedValidatorTests(unittest.TestCase):
    def test_hidden_content_and_hidden_void_element_do_not_count(self) -> None:
        hidden = " ".join(f"oculto{index}" for index in range(2_500))
        html = f"<main>uno dos<input hidden>tres<div hidden>{hidden}</div>cuatro cinco</main>"
        analysis = MODULE.analyze_html(html)
        self.assertEqual(analysis["mainWordCount"], 5)

    def test_unicode_words_count_once_and_duplicate_pages_fail(self) -> None:
        analysis = MODULE.analyze_html("<main>ano año él cotización</main>")
        self.assertEqual(analysis["mainWordCount"], 4)
        text = " ".join(f"palabra{chr(97 + index % 26)}" for index in range(100))
        pages = [
            {"url": "a", "_similarityText": text, "failures": []},
            {"url": "b", "_similarityText": text, "failures": []},
        ]
        report = MODULE.validate_pairwise(pages, 0.10, 8)
        self.assertEqual(report["violations"], 1)
        self.assertEqual(report["highest"]["overlap"], 1.0)


if __name__ == "__main__":
    unittest.main()
