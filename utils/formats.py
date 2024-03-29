"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import codecs
import datetime
import json
import re
import sys
import unicodedata
from typing import Any, Iterable, Sequence, SupportsAbs

from discord.utils import escape_markdown


CONTROL_CHARS = re.compile(
    '[%s]' % re.escape(''.join(chr(i) for i in range(sys.maxunicode) if unicodedata.category(chr(i)).startswith('C')))
)


def group(iterable: Sequence[str], page_len: int = 50) -> list[Sequence[str]]:
    pages = []
    while iterable:
        pages.append(iterable[:page_len])
        iterable = iterable[page_len:]
    return pages


class plural:
    def __init__(self, value: SupportsAbs[int]) -> None:
        self.value = value

    def __format__(self, __format_spec: str) -> str:
        v = self.value
        skip_value = __format_spec.endswith('!')
        if skip_value:
            __format_spec = __format_spec[:-1]
        singular, _, plural = __format_spec.partition('|')
        plural = plural or f'{singular}s'
        if skip_value:
            if abs(v) != 1:
                return plural
            return singular
        if abs(v) != 1:
            return f'{v} {plural}'
        return f'{v} {singular}'


class truncate:
    def __init__(self, value: str) -> None:
        self.value = value

    def __format__(self, format_spec: str) -> str:
        max_len = int(format_spec)

        if len(self.value) <= max_len:
            return self.value
        return f'{self.value[:max_len - 3]}...'


def human_join(seq: Sequence[str], delim: str = ', ', final: str = 'or') -> str:
    size = len(seq)
    if size == 0:
        return ''

    if size == 1:
        return seq[0]

    if size == 2:
        return f'{seq[0]} {final} {seq[1]}'

    return delim.join(seq[:-1]) + f' {final} {seq[-1]}'


class TabularData:
    def __init__(self) -> None:
        self._widths: list[int] = []
        self._columns = []
        self._rows: list[Any] = []

    def set_columns(self, columns: Iterable[Any]) -> None:
        self._columns = columns
        self._widths = [len(c) + 2 for c in columns]

    def add_row(self, row: Iterable[Any]):
        rows = [str(r) for r in row]
        self._rows.append(rows)
        for index, element in enumerate(rows):
            width = len(element) + 2
            if width > self._widths[index]:
                self._widths[index] = width

    def add_rows(self, rows: Iterable[Iterable[str | int]]) -> None:
        for row in rows:
            self.add_row(row)

    def render(self) -> str:
        top = '┌' + '┬'.join('─' * w for w in self._widths) + '┐'
        bottom = '└' + '┴'.join('─' * w for w in self._widths) + '┘'
        sep = '├' + '┼'.join('─' * w for w in self._widths) + '┤'

        to_draw = [top]

        def get_entry(d):
            elem = '│'.join(f'{e:^{self._widths[i]}}' for i, e in enumerate(d))
            return f'│{elem}│'

        to_draw.append(get_entry(self._columns))
        to_draw.append(sep)

        for row in self._rows:
            to_draw.append(get_entry(row))

        to_draw.append(bottom)
        return '\n'.join(to_draw)


def format_dt(dt: datetime.datetime, style: str | None = None) -> str:
    if style is None:
        return f'<t:{int(dt.timestamp())}>'
    return f'<t:{int(dt.timestamp())}:{style}>'


def to_codeblock(
    content: str, language: str = 'py', replace_existing: bool = True, escape_md: bool = False, new: str = "'''"
):
    if replace_existing:
        content = content.replace('```', new)
    if escape_md:
        content = escape_markdown(content)
    return f'```{language}\n{content}\n```'


def escape_invis(decode_error):
    decode_error.end = decode_error.start + 1
    if CONTROL_CHARS.match(decode_error.object[decode_error.start : decode_error.end]):
        return codecs.backslashreplace_errors(decode_error)
    return (decode_error.object[decode_error.start : decode_error.end].encode('utf-8'), decode_error.end)


codecs.register_error('escape-invis', escape_invis)


def escape_invis_chars(content: str) -> str:
    """Escape invisible/control characters."""
    return content.encode('ascii', 'escape-invis').decode('utf-8')


def clean_emojis(line) -> str:
    """Escape custom emojis."""
    return re.sub(r'<(a)?:([a-zA-Z0-9_]+):([0-9]+)>', '<\u200b\\1:\\2:\\3>', line)


def clean_single_backtick(line):
    """Clean string for insertion in single backtick code section.
    Clean backticks so we don't accidentally escape, and escape custom emojis
    that would be discordified.
    """
    if re.search('[^`]`[^`]', line) is not None:
        return '`%s`' % clean_double_backtick(line)
    if line[:2] == '``':
        line = '\u200b' + line
    if line[-1] == '`':
        line = line + '\u200b'
    return clean_emojis(line)


def clean_double_backtick(line) -> str:
    """Clean string for isnertion in double backtick code section.
    Clean backticks so we don't accidentally escape, and escape custom emojis
    that would be discordified.
    """
    line.replace('``', '`\u200b`')
    if line[0] == '`':
        line = '\u200b' + line
    if line[-1] == '`':
        line = line + '\u200b'

    return clean_emojis(line)


def clean_triple_backtick(line) -> str:
    """Clean string for insertion in triple backtick code section.
    Clean backticks so we don't accidentally escape, and escape custom emojis
    that would be discordified.
    """
    if not line:
        return line

    i = 0
    n = 0
    while i < len(line):
        if (line[i]) == '`':
            n += 1
        if n == 3:
            line = line[:i] + '\u200b' + line[i:]
            n = 1
            i += 1
        i += 1

    if line[-1] == '`':
        line += '\n'

    return clean_emojis(line)


def to_json(obj: Any) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=True)


def tick(opt: bool | None, /) -> str:
    lookup = {
        True: '<:yes:956843604620476457>',
        False: '<:no:956843604972826664>',
        None: '<:none:956843605010567178>',
    }
    return lookup.get(opt, '<:none:956843605010567178>')
