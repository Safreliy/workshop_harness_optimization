"""Regression coverage for exact label readout and backend capability failures."""
import importlib.util
import math
from types import SimpleNamespace
from pathlib import Path
import unittest

SOURCE = Path(__file__).parents[1] / "jev-workshop/practice/local_models.py"
spec = importlib.util.spec_from_file_location("jev_local_models", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Tokenizer:
    def apply_chat_template(self, messages, **kwargs):
        assert kwargs["enable_thinking"] is False
        return "prompt\n<think>\n</think>\n\n"

    def encode(self, text, **kwargs):
        return [ord(c) for c in text]


class TestLocalReadout(unittest.TestCase):
    def test_softmax_conditional_and_absolute_mass_are_different(self):
        probs, mass = module._distribution([math.log(.06), math.log(.04)])
        self.assertAlmostEqual(probs[0], .6)
        self.assertAlmostEqual(mass, .1)

    def test_boundary_merge_rejected(self):
        class MergeTokenizer(Tokenizer):
            def encode(self, text, **kwargs):
                return [1, 2] if not text.endswith("A") else [1, 3]
        with self.assertRaisesRegex(ValueError, "answer position"):
            module._prompt(MergeTokenizer(), "state", "criterion", {"yes": "yes", "no": "no"})

    def test_open_thinking_rejected(self):
        class OpenThinking(Tokenizer):
            def apply_chat_template(self, messages, **kwargs):
                return "<think>"
        with self.assertRaisesRegex(ValueError, "reasoning block"):
            module._prompt(OpenThinking(), "state", "criterion", {"yes": "yes", "no": "no"})

    def test_non_finite_or_invalid_temperature_rejected(self):
        for values, temperature in [([0, float("nan")], 1), ([0, -1], 0), ([0, -1], float("inf"))]:
            with self.assertRaises(ValueError):
                module._distribution(values, temperature)

    def test_encoders_reject_arbitrary_question_instead_of_pretending_to_follow_it(self):
        for scorer_class in (module.PretrainedNLIScorer, module.TrainedNLIScorer):
            scorer = scorer_class.__new__(scorer_class)
            with self.assertRaisesRegex(ValueError, "arbitrary questions"):
                scorer.classify({"request": "r", "evidence": "e", "claim": "c"}, question="Some new criterion")

    def test_trained_encoder_preserves_training_serialization_and_rejects_long_input(self):
        scorer = module.TrainedNLIScorer.__new__(module.TrainedNLIScorer)
        scorer.device, scorer.max_context_tokens = "cpu", 256
        captured = []
        class Inputs(dict):
            def to(self, device):
                return self
        def tokenize(first, second, **kwargs):
            captured.append((first, second, kwargs))
            return Inputs(input_ids=SimpleNamespace(shape=(1, 300)))
        scorer.tokenizer = tokenize
        with self.assertRaisesRegex(ValueError, "No silent truncation"):
            scorer.classify({"request": "r", "evidence": "e", "claim": "c"})
        self.assertEqual(captured[0][0], "Запрос пользователя:\nr\n\nНаблюдения инструментов:\ne")
        self.assertEqual(captured[0][1], "Проверяемое утверждение агента:\nc")
        self.assertFalse(captured[0][2]["truncation"])


if __name__ == "__main__":
    unittest.main()
