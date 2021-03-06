"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

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
