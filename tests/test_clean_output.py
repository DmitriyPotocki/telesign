from bot.telegram.formatting import clean_output, limit_text


def test_removes_thinking_blocks():
    text = "<thinking>internal plan</thinking>Готов ответ: сделаем дуб."
    cleaned = clean_output(text)
    assert "internal plan" not in cleaned
    assert "дуб" in cleaned


def test_removes_tool_call_blocks():
    tag = "antml:" + "invoke"  # service tag built at runtime
    text = f"Готово.\n<{tag} name=\"submit_plan\">{{\"action\":\"chat\"}}</{tag}>Готово."
    cleaned = clean_output(text)
    assert "submit_plan" not in cleaned
    assert "Готово." in cleaned


def test_removes_stray_service_tags():
    text = "</thinking>Привет!\n<function_calls></function_calls>Как дела?"
    cleaned = clean_output(text)
    assert "<thinking" not in cleaned
    assert "function_calls" not in cleaned
    assert "Привет!" in cleaned


def test_removes_json_tool_call_object():
    text = '{"action": "generate", "jobs": []}'
    assert clean_output(text) == ""


def test_removes_model_chatter():
    text = "Сейчас вызову функцию планирования.\nВот варианты."
    cleaned = clean_output(text)
    assert "вызову функцию" not in cleaned
    assert "варианты" in cleaned


def test_collapse_blank_lines():
    assert clean_output("a\n\n\n\n\nb") == "a\n\nb"


def test_plain_text_untouched():
    text = "Просто текст про дуб и стены."
    assert clean_output(text) == text


def test_empty_input():
    assert clean_output("") == ""


def test_limit_cut_on_paragraph():
    long = "abc\n\ndef" + "x" * 5000
    limited = limit_text(long, 100)
    assert len(limited) <= 101
    assert limited.endswith("…")
