"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, TypedDict

if TYPE_CHECKING:
    from aiohttp import ClientSession


LANGUAGES = {
    'af': 'Afrikaans',
    'sq': 'Albanian',
    'am': 'Amharic',
    'ar': 'Arabic',
    'hy': 'Armenian',
    'az': 'Azerbaijani',
    'eu': 'Basque',
    'be': 'Belarusian',
    'bn': 'Bengali',
    'bs': 'Bosnian',
    'bg': 'Bulgarian',
    'ca': 'Catalan',
    'ceb': 'Cebuano',
    'ny': 'Chichewa',
    'zh-cn': 'Chinese (Simplified)',
    'zh-tw': 'Chinese (Traditional)',
    'co': 'Corsican',
    'hr': 'Croatian',
    'cs': 'Czech',
    'da': 'Danish',
    'nl': 'Dutch',
    'en': 'English',
    'eo': 'Esperanto',
    'et': 'Estonian',
    'tl': 'Filipino',
    'fi': 'Finnish',
    'fr': 'French',
    'fy': 'Frisian',
    'gl': 'Galician',
    'ka': 'Georgian',
    'de': 'German',
    'el': 'Greek',
    'gu': 'Gujarati',
    'ht': 'Haitian Creole',
    'ha': 'Hausa',
    'haw': 'Hawaiian',
    'iw': 'Hebrew',
    'he': 'Hebrew',
    'hi': 'Hindi',
    'hmn': 'Hmong',
    'hu': 'Hungarian',
    'is': 'Icelandic',
    'ig': 'Igbo',
    'id': 'Indonesian',
    'ga': 'Irish',
    'it': 'Italian',
    'ja': 'Japanese',
    'jw': 'Javanese',
    'kn': 'Kannada',
    'kk': 'Kazakh',
    'km': 'Khmer',
    'ko': 'Korean',
    'ku': 'Kurdish (Kurmanji)',
    'ky': 'Kyrgyz',
    'lo': 'Lao',
    'la': 'Latin',
    'lv': 'Latvian',
    'lt': 'Lithuanian',
    'lb': 'Luxembourgish',
    'mk': 'Macedonian',
    'mg': 'Malagasy',
    'ms': 'Malay',
    'ml': 'Malayalam',
    'mt': 'Maltese',
    'mi': 'Maori',
    'mr': 'Marathi',
    'mn': 'Mongolian',
    'my': 'Myanmar (Burmese)',
    'ne': 'Nepali',
    'no': 'Norwegian',
    'or': 'Odia',
    'ps': 'Pashto',
    'fa': 'Persian',
    'pl': 'Polish',
    'pt': 'Portuguese',
    'pa': 'Punjabi',
    'ro': 'Romanian',
    'ru': 'Russian',
    'sm': 'Samoan',
    'gd': 'Scots Gaelic',
    'sr': 'Serbian',
    'st': 'Sesotho',
    'sn': 'Shona',
    'sd': 'Sindhi',
    'si': 'Sinhala',
    'sk': 'Slovak',
    'sl': 'Slovenian',
    'so': 'Somali',
    'es': 'Spanish',
    'su': 'Sundanese',
    'sw': 'Swahili',
    'sv': 'Swedish',
    'tg': 'Tajik',
    'ta': 'Tamil',
    'te': 'Telugu',
    'th': 'Thai',
    'tr': 'Turkish',
    'uk': 'Ukrainian',
    'ur': 'Urdu',
    'ug': 'Uyghur',
    'uz': 'Uzbek',
    'vi': 'Vietnamese',
    'cy': 'Welsh',
    'xh': 'Xhosa',
    'yi': 'Yiddish',
    'yo': 'Yoruba',
    'zu': 'Zulu',
}

FLAG_TO_LANG: dict[str, str] = {
    '🇦🇫': 'ps',
    '🇸🇦': 'ar',
    '🇦🇪': 'ar',
    '🇦🇱': 'sq',
    '🇦🇲': 'hy',
    '🇦🇺': 'en',
    '🇦🇿': 'az',
    '🇧🇾': 'be',
    '🇧🇦': 'bs',
    '🇧🇷': 'pt',
    '🇧🇬': 'bg',
    '🇰🇭': 'km',
    '🇨🇳': 'zh-cn',
    '🇭🇷': 'hr',
    '🇨🇿': 'cs',
    '🇩🇰': 'da',
    '🇪🇬': 'ar',
    '🇪🇪': 'et',
    '🇪🇹': 'am',
    '🇫🇮': 'fi',
    '🇫🇷': 'fr',
    '🇬🇪': 'ka',
    '🇩🇪': 'de',
    '🇬🇷': 'el',
    '🇭🇹': 'ht',
    '🇭🇰': 'zh-tw',
    '🇭🇺': 'hu',
    '🇮🇸': 'is',
    '🇮🇳': 'hi',
    '🇮🇩': 'id',
    '🇮🇷': 'fa',
    '🇮🇪': 'ga',
    '🇮🇱': 'he',
    '🇮🇹': 'it',
    '🇯🇵': 'ja',
    '🇰🇿': 'kk',
    '🇰🇪': 'sw',
    '🇰🇬': 'ky',
    '🇱🇦': 'lo',
    '🇱🇻': 'lv',
    '🇱🇸': 'st',
    '🇱🇹': 'lt',
    '🇱🇺': 'lb',
    '🇲🇰': 'mk',
    '🇲🇬': 'mg',
    '🇲🇼': 'ny',
    '🇲🇾': 'ms',
    '🇲🇹': 'mt',
    '🇲🇽': 'es',
    '🇲🇳': 'mn',
    '🇲🇲': 'my',
    '🇳🇦': 'af',
    '🇳🇵': 'ne',
    '🇳🇱': 'nl',
    '🇳🇬': 'yo',
    '🇳🇴': 'no',
    '🇵🇰': 'ur',
    '🇵🇸': 'ar',
    '🇵🇭': 'tl',
    '🇵🇱': 'pl',
    '🇵🇹': 'pt',
    '🇷🇴': 'ro',
    '🇷🇺': 'ru',
    '🇼🇸': 'sm',
    '🇷🇸': 'sr',
    '🇸🇰': 'sk',
    '🇸🇮': 'sl',
    '🇸🇴': 'so',
    '🇿🇦': 'zu',
    '🇰🇷': 'ko',
    '🇪🇸': 'es',
    '🇱🇰': 'si',
    '🇸🇪': 'sv',
    '🇹🇼': 'zh-tw',
    '🇹🇯': 'tg',
    '🇹🇭': 'th',
    '🇹🇷': 'tr',
    '🇺🇦': 'uk',
    '🇬🇧': 'en',
    '🏴󠁧󠁢󠁥󠁮󠁧󠁿': 'en',  # england
    '🏴󠁧󠁢󠁷󠁬󠁳󠁿': 'cy',  # wales
    '🏴󠁧󠁢󠁳󠁣󠁴󠁿': 'gd',  # scotland
    '🇺🇸': 'en',
    '🇺🇿': 'uz',
    '🇻🇳': 'vi',
    '🇿🇼': 'sn',
    '🇺🇲': 'en',
}

LANG_TO_FLAG: dict[str, str] = {}

for flag, lang in FLAG_TO_LANG.items():
    if lang not in LANG_TO_FLAG:
        LANG_TO_FLAG[lang] = flag
LANG_TO_FLAG['en'] = '🇬🇧'


class TranslateError(Exception):
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text
        super().__init__(f'Google responded with HTTP Status Code {status_code}')


class TranslatedSentence(TypedDict):
    trans: str
    orig: str


class TranslateResult(NamedTuple):
    original: str
    translated: str
    source_language: str
    target_language: str


async def translate(text: str, *, src: str = 'auto', dest: str = 'en', session: ClientSession) -> TranslateResult:
    # This was discovered by the people here:
    # https://github.com/ssut/py-googletrans/issues/268
    query = {
        'dj': '1',
        'dt': ['sp', 't', 'ld', 'bd'],
        'client': 'dict-chrome-ex',
        # Source Language
        'sl': src,
        # Target Language
        'tl': dest,
        # Query
        'q': text,
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36'
    }

    target_language = LANGUAGES.get(dest, 'Unknown')

    async with session.get('https://clients5.google.com/translate_a/single', params=query, headers=headers) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise TranslateError(resp.status, text)

        data = await resp.json()
        src = data.get('src', 'Unknown')
        source_language = LANGUAGES.get(src, src)
        sentences: list[TranslatedSentence] = data.get('sentences', [])
        if len(sentences) == 0:
            raise RuntimeError('Google translate returned no information')

        original = ''.join(sentence.get('orig', '') for sentence in sentences)
        translated = ''.join(sentence.get('trans', '') for sentence in sentences)

        return TranslateResult(
            original=original,
            translated=translated,
            source_language=source_language,
            target_language=target_language,
        )