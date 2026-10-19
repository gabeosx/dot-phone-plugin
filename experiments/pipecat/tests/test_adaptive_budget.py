from adaptive_receptionist import voice_cost


def test_voice_estimate_prices_both_modalities_and_cached_history():
    usage = {
        "response": {
            "input_token_details": {
                "text_tokens": 1000,
                "audio_tokens": 2000,
                "cached_tokens_details": {"text_tokens": 800, "audio_tokens": 1500},
            },
            "output_token_details": {"text_tokens": 100, "audio_tokens": 300},
        },
        "cancelled": None,
    }
    assert round(voice_cost(usage), 8) == 0.03932
    assert voice_cost({}) == 0
