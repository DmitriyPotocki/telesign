"""Output hygiene and Markdown -> Telegram HTML conversion.

Everything the user sees as text goes through clean_output() / md_to_html().
"""
from __future__ import annotations

import html
import re

# --- service artifacts that must never reach the user -------------------------
_SERVICE_TAGS = [
    "thinking",
    "thought",
    "antml:thinking",
    "invoke",
    "function_calls",
    "function_results",
    "tool_call",
    "tool_calls",
    "tool_response",
    "system",
]

_TAG_BLOCK_RE = re.compile(
    r"<\s*/?\s*(?:" + "|".join(map(re.escape, _SERVICE_TAGS)) + r")\b[^>]*>", re.IGNORECASE
)
# <invoke name="...">…</invoke> and the inner argument tags
_INNER_INVOKE_RE = re.compile(
    r"<\s*/?\s*(?:antml:)?invoke\b[^>]*>|<\s*/?\s*(?:antml:)?parameter\b[^>]*>",
    re.IGNORECASE,
)

# full <invoke>…</invoke> pair (drop whole content, incl. antml: prefix)
_INV = "inv" + "oke"
_INVOKE_BLOCK_RE = re.compile(
    r"<\s*/?\s*(?:antml:)?" + _INV + r"\b[^>]*>.*?</\s*\S*?" + _INV + r"\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INNER_TOOL_RE = re.compile(
    r"<(?P<tag>thinking|thought|tool_call|tool_calls|function_calls|function_results)"
    r"[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# a JSON object that is clearly a tool call / plan payload
_ORPHAN_JSON_CALL_RE = re.compile(
    r"""^\s*\{[\s\S]*"(?:action|jobs|name|tool|tool_name|function)"[\s\S]*\}\s*$"""
)

_ALLOWED_TAGS = ("b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote")


# --- clean_output -------------------------------------------------------------
def clean_output(text: str) -> str:
    """Remove thinking/tool blocks, service tags, service phrases, extra blank lines."""
    if not text:
        return ""

    # drop fenced <think> style blocks first (inner content too)
    text = _INVOKE_BLOCK_RE.sub("", text)
    text = _INNER_TOOL_RE.sub("", text)
    # drop any stray opening/closing service tags
    text = _TAG_BLOCK_RE.sub("", text)
    # a raw JSON tool-call object on its own line
    text = _ORPHAN_JSON_CALL_RE.sub("", text)

    # common model chatter
    chatter = [
        r"^\s*сейчас\s+вызову[^\n]*$",
        r"^\s*сейчас\s+запущ[^\n]*$",
        r"^\s*сейчас\s+сдела[^\n]*$",
        r"^\s*сейчас\s+покажу[^\n]*$",
        r"^\s*(вызов\s+инструмента|вызываю\s+инструмент)[^\n]*$",
    ]
    for pat in chatter:
        text = re.sub(pat, "", text, flags=re.IGNORECASE | re.MULTILINE)

    # stray tags like <json>, <plan> etc: unwrap them (keep content), remove unknown tags
    text = re.sub(r"<(json|plan|output|answer)\b[^>]*>(.*?)</\1\s*>", r"\2", text, flags=re.DOTALL | re.IGNORECASE)

    # markdown-style fences that leaked (keep content, drop the fence lines)
    text = re.sub(r"^\s*```[\w-]*\s*$", "", text, flags=re.MULTILINE)

    # repeated preambles like "Вот ответ:" several times in a row
    text = re.sub(r"(?m)^(?:вот\s+(?:ответ|результат|вариант[ы]?)[!.:—-]*\s*){2,}", "", text, flags=re.IGNORECASE)

    # collapse >1 blank line
    text = re.sub(r"\n{3,}", "\n\n", text)

    # if the only remaining content was service stuff
    if not text.strip():
        return ""
    return text.strip()


# --- markdown -> html ---------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_UNDERLINE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$", re.MULTILINE)
_MD_NUM_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$", re.MULTILINE)
_MD_CODE_BLOCK_RE = re.compile(r"```(?P<lang>[\w+-]*)\n?(?P<body>.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BLOCKQUOTE_RE = re.compile(r"^(>\s?)(.*)$", re.MULTILINE)


def _balance_entities(s: str) -> str:
    """Close incomplete Telegram HTML entities in a streaming partial."""
    # tag stack of allowed tags
    stack: list[str] = []
    for m in re.finditer(r"<(/?)(b|strong|i|em|u|s|code|pre|blockquote|a)\b[^>]*>", s):
        closing, tag = m.group(1), m.group(2)
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    # unmatched opening tag at the very end may be truncated mid-tag: cut it
    trunc = re.search(r"<[^>]*$", s)
    if trunc:
        s = s[: trunc.start()]
        # recompute stack after cutting
        stack = []
        for m in re.finditer(r"<(/?)(b|strong|i|em|u|s|code|pre|blockquote|a)\b[^>]*>", s):
            closing, tag = m.group(1), m.group(2)
            if closing:
                if stack and stack[-1] == tag:
                    stack.pop()
            else:
                stack.append(tag)
    return s + "".join(f"</{t}>" for t in reversed(stack))


def _is_balanced(s: str) -> bool:
    stack: list[str] = []
    for m in re.finditer(r"<(/?)(b|strong|i|em|u|s|code|pre|blockquote|a)\b[^>]*>", s):
        closing, tag = m.group(1), m.group(2)
        if closing:
            if not stack or stack[-1] != tag:
                return False
            stack.pop()
        else:
            stack.append(tag)
    return not stack


def md_to_html(md_text: str, *, partial: bool = False) -> str:
    """Convert limited Markdown to Telegram-compatible HTML (parse_mode=HTML).

    Supports: bold, italic, underline, strike, inline code, fenced code blocks,
    blockquotes, links, bullets and numbered lists. Headings -> bold. Tables are
    not used. With partial=True, incomplete markup is closed so Telegram never
    sees "can't parse entities" mid-stream.
    """
    text = md_text or ""

    # --- 1) stash fenced code blocks and protect markup we insert ourselves ---
    blocks: list[str] = []

    def _stash_code(m: re.Match) -> str:
        body = html.escape(m.group("body") or "", quote=False)
        blocks.append(f"<pre><code>{body}</code></pre>")
        return f"\x00CB{len(blocks) - 1}\x00"

    text = _MD_CODE_BLOCK_RE.sub(_stash_code, text)

    protected: list[str] = []

    def _protect(html_fragment: str) -> str:
        protected.append(html_fragment)
        return f"\x00PROT{len(protected) - 1}\x00"

    # headings -> bold line (protect from escaping)
    text = _MD_HEADING_RE.sub(
        lambda m: _protect(f"<b>{html.escape(m.group(2).strip(), quote=False)}</b>"), text
    )
    # blockquotes
    text = _MD_BLOCKQUOTE_RE.sub(
        lambda m: _protect(f"<blockquote>{html.escape(m.group(2), quote=False)}</blockquote>"), text
    )
    # bullets / numbers: markers are plain text, keep them as-is
    text = _MD_BULLET_RE.sub(lambda m: f"{m.group(1)}• {m.group(2)}", text)
    text = _MD_NUM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}. {m.group(3)}", text)

    # --- 2) escape the user text, then apply inline markdown -> protected tags ---
    text = html.escape(text, quote=False)

    text = _MD_UNDERLINE_RE.sub(lambda m: _protect("<u>" + m.group(1) + "</u>"), text)
    text = _MD_STRIKE_RE.sub(lambda m: _protect("<s>" + m.group(1) + "</s>"), text)
    text = _MD_BOLD_RE.sub(lambda m: _protect("<b>" + m.group(1) + "</b>"), text)
    text = _MD_ITALIC_RE.sub(lambda m: _protect("<i>" + m.group(1) + "</i>"), text)
    text = _MD_INLINE_CODE_RE.sub(lambda m: _protect("<code>" + m.group(1) + "</code>"), text)
    text = _MD_LINK_RE.sub(
        lambda m: _protect(
            '<a href="' + html.escape(m.group(2), quote=True) + '">' + m.group(1) + "</a>"
        ),
        text,
    )

    # --- 3) restore protected fragments and code blocks ---
    text = re.sub("\\x00PROT(\\d+)\\x00", lambda m: protected[int(m.group(1))], text)

    def _unstash(m: re.Match) -> str:
        return blocks[int(m.group(1))]

    text = re.sub("\\x00CB(\\d+)\\x00", _unstash, text)

    # 4) merge consecutive blockquote lines into one block
    text = re.sub(r"</blockquote>\s*<blockquote>", "\n", text)

    if partial and not _is_balanced(text):
        text = _balance_entities(text)
    return text


def strip_html_for_fallback(md_text: str) -> str:
    """Plain-text version used when Telegram rejects our HTML."""
    text = md_to_html(md_text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def limit_text(text: str, limit: int = 4096) -> str:
    """Hard limit with cut on paragraph boundaries."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # try to cut at last paragraph break
    for sep in ("\n\n", "\n", " ", ""):
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            return cut[:idx].rstrip() + "…"
    return cut.rstrip() + "…"
