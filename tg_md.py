"""Markdown ⇄ Telegram formatting helpers.

Copied from the user's telegram MCP server
(~/news_telegram_bot/general_assistant/mcp_server.py) for reuse by the
opencode-remote bridge, with ONE deliberate deviation per the owner's request:
the RTL mark (\u200f) that the MCP version injects into every line is NOT
added here — the bridge carries a lot of English/LTR text, and the forced
right-alignment hurts readability there.

  markdown_to_tg_html(text)            — markdown → Telegram HTML
  tg_entities_to_markdown(text, ents)  — Telegram message entities → markdown
"""

from __future__ import annotations

import html
import re


def _has_rtl_chars(text: str) -> bool:
    """Return True if *text* contains Hebrew, Arabic or other RTL characters."""
    for ch in text:
        if '\u0590' <= ch <= '\u05ff':   # Hebrew
            return True
        if '\u0600' <= ch <= '\u06ff':   # Arabic
            return True
        if '\ufb50' <= ch <= '\ufdff':   # Arabic Presentation Forms-A
            return True
        if '\ufe70' <= ch <= '\ufeff':   # Arabic Presentation Forms-B
            return True
        if '\ufb1d' <= ch <= '\ufb4f':   # Hebrew Presentation Forms
            return True
    return False


def _render_md_table(table_lines: list[str]) -> str:
    """Convert markdown table lines into a Telegram-friendly format.

    * **LTR tables** → monospace ``<pre>`` block with box-drawing chars.
    * **RTL tables** (Hebrew / Arabic) → styled list with ``·`` separators,
      since Telegram monospace fonts don't give RTL characters consistent
      widths and column alignment breaks completely.
    """
    rows: list[list[str]] = []
    for line in table_lines:
        stripped = line.strip()
        # Skip separator rows (e.g. |---|---|)
        if stripped and re.match(r'^\|?[\s\-:]+([\s\-:]+\|)+[\s\-:]*\|?$', stripped):
            continue
        cells = [c.strip() for c in stripped.strip('|').split('|')]
        rows.append(cells)

    if not rows:
        return '\n'.join(table_lines)

    # Normalise column count
    num_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < num_cols:
            r.append('')

    # Detect RTL content anywhere in the table
    all_text = ' '.join(cell for row in rows for cell in row)
    is_rtl = _has_rtl_chars(all_text)

    if is_rtl:
        return _render_rtl_table(rows)
    else:
        return _render_ltr_table(rows)


def _render_rtl_table(rows: list[list[str]]) -> str:
    """Render a table with RTL content as a styled list.

    Header row becomes bold, data rows use bullet separators.
    No column alignment is attempted — just clean separators.
    """
    lines: list[str] = []
    sep = '  ·  '

    if len(rows) >= 1:
        # Header row — bold
        header = rows[0]
        header_text = sep.join(f'<b>{html.escape(h)}</b>' for h in header if h)
        lines.append(f'📊 {header_text}')
        lines.append('━━━━━━━━━━━━━━━━━━━━━━')

    for row in rows[1:]:
        escaped = [html.escape(c) for c in row]
        row_text = sep.join(escaped)
        lines.append(f'▫️ {row_text}')

    return '\n'.join(lines)


def _render_ltr_table(rows: list[list[str]]) -> str:
    """Render an LTR table as a monospace <pre> block with box-drawing."""
    num_cols = len(rows[0]) if rows else 0

    # Compute column widths (min 3 for aesthetics)
    col_widths = [3] * num_cols
    for r in rows:
        for i, cell in enumerate(r):
            col_widths[i] = max(col_widths[i], len(cell) + 2)  # +2 padding

    # Box-drawing helpers
    def hline(left, mid, right, fill='─'):
        parts = [fill * w for w in col_widths]
        return left + mid.join(parts) + right

    def data_row(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(' ' + cell.ljust(col_widths[i] - 1))
        return '│' + '│'.join(parts) + '│'

    lines_out: list[str] = []
    lines_out.append(hline('┌', '┬', '┐'))

    for idx, row in enumerate(rows):
        lines_out.append(data_row(row))
        if idx == 0 and len(rows) > 1:
            # Bold separator after header
            lines_out.append(hline('├', '┼', '┤', '═'))
        elif idx < len(rows) - 1:
            lines_out.append(hline('├', '┼', '┤'))

    lines_out.append(hline('└', '┴', '┘'))

    table_text = html.escape('\n'.join(lines_out))
    return f'<pre>{table_text}</pre>'


def markdown_to_tg_html(text: str) -> str:
    """Parses standard markdown to Telegram-compatible HTML.

    Adapted for the bridge: no RTL (\u200f) marks are injected.
    """

    # 1. Extract code blocks
    code_blocks = []
    def code_block_repl(match):
        lang = match.group(1).strip()
        code = match.group(2)
        code = html.escape(code)
        placeholder = f"@@CODEBLOCK{len(code_blocks)}@@"
        if lang:
            code_blocks.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
        else:
            code_blocks.append(f'<pre><code>{code}</code></pre>')
        return placeholder

    text = re.sub(r'```([a-zA-Z0-9_-]*)\n(.*?)```', code_block_repl, text, flags=re.DOTALL)

    # 2. Extract inline code
    inline_codes = []
    def inline_code_repl(match):
        code = html.escape(match.group(1))
        placeholder = f"@@INLINECODE{len(inline_codes)}@@"
        inline_codes.append(f'<code>{code}</code>')
        return placeholder
    text = re.sub(r'`([^`]+)`', inline_code_repl, text)

    # 2b. Extract and render markdown tables
    table_blocks: list[str] = []
    def _replace_tables(text: str) -> str:
        """Find consecutive lines that form markdown tables, render them,
        and replace with placeholders."""
        out_lines: list[str] = []
        table_buf: list[str] = []

        def flush_table():
            nonlocal table_buf
            if len(table_buf) >= 2:
                placeholder = f"@@TABLE{len(table_blocks)}@@"
                table_blocks.append(_render_md_table(table_buf))
                out_lines.append(placeholder)
            else:
                out_lines.extend(table_buf)
            table_buf = []

        for line in text.split('\n'):
            stripped = line.strip()
            # A table line contains at least one pipe that isn't inside a placeholder
            is_table_line = ('|' in stripped
                            and not stripped.startswith('@@')
                            and (stripped.startswith('|')
                                 or re.match(r'^[^|]+\|', stripped)))
            if is_table_line:
                table_buf.append(line)
            else:
                if table_buf:
                    flush_table()
                out_lines.append(line)

        if table_buf:
            flush_table()

        return '\n'.join(out_lines)

    text = _replace_tables(text)

    # 3. Escape text for HTML safety
    text = html.escape(text)

    # 4. Extract links safely
    links = []
    def link_repl(match):
        link_text = match.group(1)
        url = match.group(2)
        placeholder = f"@@LINK{len(links)}@@"
        links.append(f'<a href="{url}">{link_text}</a>')
        return placeholder
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link_repl, text)

    # 5. Markdown format patterns
    # Headers - creative substitution since TG doesn't support <h> tags
    text = re.sub(r'^#\s+(.*)$', r'<b>📢 \1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+(.*)$', r'<b>🔹 \1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^###\s+(.*)$', r'<b>🔸 \1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^####+\s+(.*)$', r'<b>▪️ \1</b>', text, flags=re.MULTILINE)

    # Horizontal Rule (Telegram has no native HR, using line characters)
    text = re.sub(r'^---+$', r'───────────────────────────', text, flags=re.MULTILINE)

    # Blockquotes
    def blockquote_repl(match):
        content = match.group(1)
        return f'<blockquote>{content}</blockquote>'
    text = re.sub(r'^&gt;\s?(.*)$', blockquote_repl, text, flags=re.MULTILINE)

    # Text decorators
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<u>\1</u>', text)
    text = re.sub(r'(?<!\*)\*(.*?)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!_)_(.*?)_(?!_)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)

    # Lists
    text = re.sub(r'^\*\s+(.*)$', r'• \1', text, flags=re.MULTILINE)
    text = re.sub(r'^-\s+(.*)$', r'• \1', text, flags=re.MULTILINE)

    # (bridge deviation) NO step 6: the MCP version injects \u200f (RLM) into
    # every line to right-align Hebrew; omitted here — this feed is mostly LTR.

    # 7. Restore placeholders
    for i, link_html in enumerate(links):
        text = text.replace(f"@@LINK{i}@@", link_html)
    for i, code_html in enumerate(inline_codes):
        text = text.replace(f"@@INLINECODE{i}@@", code_html)
    for i, block_html in enumerate(code_blocks):
        text = text.replace(f"@@CODEBLOCK{i}@@", block_html)
    for i, table_html in enumerate(table_blocks):
        text = text.replace(f"@@TABLE{i}@@", table_html)

    return text


def tg_entities_to_markdown(text: str, entities: list) -> str:
    """Converts a telegram message with entities back to markdown."""
    if not entities:
        return text

    # Sort entities by offset in reverse order to avoiding messing up offsets
    entities_sorted = sorted(entities, key=lambda e: e['offset'], reverse=True)

    # Telegram offsets and lengths are in UTF-16 code units
    text_utf16 = text.encode('utf-16-le')

    for entity in entities_sorted:
        offset = entity['offset'] * 2
        length = entity['length'] * 2

        prefix = text_utf16[:offset]
        content = text_utf16[offset:offset+length]
        suffix = text_utf16[offset+length:]

        etype = entity['type']
        content_str = content.decode('utf-16-le')

        if etype == 'bold':
            new_content = f"**{content_str}**"
        elif etype == 'italic':
            new_content = f"*{content_str}*"
        elif etype == 'underline':
            new_content = f"__{content_str}__"
        elif etype == 'strikethrough':
            new_content = f"~~{content_str}~~"
        elif etype == 'code':
            new_content = f"`{content_str}`"
        elif etype == 'pre':
            lang = entity.get('language', '')
            new_content = f"```{lang}\n{content_str}\n```"
        elif etype in ('blockquote', 'expandable_blockquote'):
            lines = content_str.split('\n')
            new_content = '\n'.join(f"> {line}" if line.strip() else ">" for line in lines)
        elif etype == 'text_link':
            new_content = f"[{content_str}]({entity.get('url', '')})"
        else:
            new_content = content_str

        text_utf16 = prefix + new_content.encode('utf-16-le') + suffix

    return text_utf16.decode('utf-16-le')
