from types import SimpleNamespace

from virgo.ai.observed_usage import anthropic_usage, chat_usage, empty_usage, responses_usage


def test_responses_preserves_zero_and_missing_and_reasoning_subset():
    assert responses_usage({}) == empty_usage()
    assert responses_usage({"input_tokens": 20, "output_tokens": 8,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 3}}) == {
        "input_tokens": 20, "cached_input_tokens": 0, "cache_write_tokens": None,
        "output_tokens": 8, "reasoning_tokens": 3,
    }


def test_chat_compatible_cache_counter_and_reasoning():
    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=8, prompt_cache_hit_tokens=5,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=3))
    assert chat_usage(usage) == {"input_tokens": 20, "cached_input_tokens": 5, "cache_write_tokens": None,
                                 "output_tokens": 8, "reasoning_tokens": 3}
    assert chat_usage(SimpleNamespace()) == empty_usage()


def test_anthropic_disjoint_input_and_unknown_cache():
    usage = dict(input_tokens=10, cache_read_input_tokens=7, cache_creation_input_tokens=3, output_tokens=4)
    assert anthropic_usage(usage)["input_tokens"] == 20
    assert anthropic_usage({"input_tokens": 10})["input_tokens"] is None
    assert anthropic_usage({"input_tokens": 0, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0})["input_tokens"] == 0
