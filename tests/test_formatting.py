from bot.telegram.formatting import md_to_html, strip_html_for_fallback


def test_bold_italic_code():
    out = md_to_html("**жирный** *курсив* `код`")
    assert out == "<b>жирный</b> <i>курсив</i> <code>код</code>"


def test_headings_to_bold():
    out = md_to_html("# Заголовок")
    assert "<b>Заголовок</b>" in out
    assert "#" not in out


def test_bullets_and_numbering():
    out = md_to_html("- item\n1. one")
    assert "• item" in out
    assert "1. one" in out


def test_blockquote():
    out = md_to_html("> цитата")
    assert out == "<blockquote>цитата</blockquote>"


def test_links():
    out = md_to_html("[ссылка](https://example.com)")
    assert '<a href="https://example.com">ссылка</a>' in out


def test_code_block_preserved_and_escaped():
    out = md_to_html("```\n<b>not bold</b>\n```")
    assert "<pre><code>" in out
    assert "&lt;b&gt;not bold&lt;/b&gt;" in out


def test_escaping_of_raw_html():
    out = md_to_html("текст <b>сирый тег</b> конец")
    assert "<sирый" not in out
    assert "&lt;b&gt;" in out


def test_partial_closes_unbalanced_tags():
    partial = md_to_html("**жирный и *курсив", partial=True)
    stack_open = partial.count("<b>") - partial.count("</b>")
    stack_open += partial.count("<i>") - partial.count("</i>")
    assert stack_open == 0


def test_partial_truncates_mid_tag():
    partial = md_to_html("текст <b", partial=True)
    assert "<b" not in partial


def test_complete_text_balanced_even_partial():
    partial = md_to_html("**жирный** обычный", partial=True)
    assert partial == "<b>жирный</b> обычный"


def test_strip_html_for_fallback():
    out = strip_html_for_fallback("**жирный** и [a](https://b.c)")
    assert "<" not in out
    assert "жирный" in out
