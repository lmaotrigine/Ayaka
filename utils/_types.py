"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from typing import Literal, TypeAlias, TypedDict

import discord

from typing_extensions import NotRequired


__all__ = (
    'MessageableGuildChannel',
    'KanjiDevKanjiPayload',
    'KanjiDevWordsPayload',
    'KanjiDevReadingPayload',
    'JishoWordsPayload',
    'JishoWordsResponse',
)

MessageableGuildChannel: TypeAlias = discord.TextChannel | discord.Thread | discord.VoiceChannel


class KanjiDevKanjiPayload(TypedDict):
    kanji: str
    grade: int | None
    stroke_count: int
    meanings: list[str]
    kun_readings: list[str]
    on_readings: list[str]
    name_readings: list[str]
    jlpt: int | None
    unicode: str
    heisig_en: str | None


class _KanjiDevMeanings(TypedDict):
    glosses: list[str]


class _KanjiDevVariants(TypedDict):
    written: str
    pronounced: str
    priorities: list[str]


class KanjiDevWordsPayload(TypedDict):
    meanings: list[_KanjiDevMeanings]
    variants: list[_KanjiDevVariants]


class KanjiDevReadingPayload(TypedDict):
    reading: str
    main_kanji: list[str]
    name_kanji: list[str]


class _JishoSenses(TypedDict):
    antonyms: list[str]
    english_definitions: list[str]
    info: list[str]
    links: list[dict[str, str]]
    parts_of_speech: list[str]
    restrictions: list[str]
    see_also: list[str]
    source: list[dict[str, str]]
    tags: list[str]


class _JishoJapanesePayload(TypedDict):
    word: str
    reading: str


class _JishoAttributions(TypedDict):
    jmdict: bool
    jmnedict: bool
    dbpedia: str | None


class JishoWordsPayload(TypedDict):
    slug: str
    is_common: bool
    tags: list[str]
    jlpt: list[str]
    japanese: list[_JishoJapanesePayload]
    senses: list[_JishoSenses]
    attribution: _JishoAttributions


class JishoWordsResponse(TypedDict):
    meta: dict[Literal['status'], Literal[200, 404]]
    data: list[JishoWordsPayload]


class _DndClassHD(TypedDict):
    number: int
    faces: int


_DndClassSkillsChoice = TypedDict(
    '_DndClassSkillsChoice',
    {
        'from': list[str],
        'count': int,
    }
)

class _DndClassStartingProficiencies(TypedDict):
    armor: list[str]
    weapons: list[str]
    tools: list[str]
    skills: _DndClassSkillsChoice


class _DndClassStartingEquipmentBField(TypedDict):
    equipmentType: str
    quantity: int


class _DndClassStartingEquipmentDefaultData(TypedDict):
    a: NotRequired[list[str]]
    b: NotRequired[list[_DndClassStartingEquipmentBField]]
    _: NotRequired[list[str]]


class _DndClassStartingEquipment(TypedDict):
    additionalFromBackground: bool
    default: list[str]
    defaultData: _DndClassStartingEquipmentDefaultData


class _DndClassTableGroups(TypedDict):
    collabels: list[str]
    rows: list[list[int]]


class _DndClassFeatures(TypedDict):
    classFeature: str
    gainSubclassFeature: bool


class _DndClass(TypedDict):
    name: str
    source: str
    page: int
    isReprinted: bool
    hd: _DndClassHD
    proficiency: list[str]
    spellcastingAbility: str
    casterProgression: str
    spellsKnownProgression: list[int]
    startingProficiencies: _DndClassStartingProficiencies
    startingEquipment: _DndClassStartingEquipment
    classTableGroups: _DndClassTableGroups
    classFeatures: list[str | _DndClassFeatures]
    subclassTitle: str


class _DndSubclass(TypedDict):
    name: str
    shortName: str
    source: str
    className: str
    classSource: str
    page: int
    subclassFeatures: list[str]


class _DndClassFeatureOtherSource(TypedDict):
    source: str
    page: int


class _DndClassFeatureEntry(TypedDict):
    type: str
    items: list[str]


class _DndClassFeature(TypedDict):
    name: str
    source: str
    page: int
    otherSources: list[_DndClassFeatureOtherSource]
    className: str
    classSource: str
    level: int
    entries: list[str | _DndClassFeatureEntry]


class _DndSubclassFeatureEntry(TypedDict):
    type: str
    subclassFeature: str


class _DndSubclassFeature(TypedDict):
    name: str
    source: str
    page: int
    otherSources: _DndClassFeatureOtherSource
    className: str
    classSource: str
    subclassShortName: str
    subclassSource: str
    level: int
    entries: list[str | _DndSubclassFeatureEntry]


DndClassTopLevel = TypedDict(
    'DndClassTopLevel',
    {
        'class': list[_DndClass],
        'subclass': list[_DndSubclass],
        'classFeature': list[_DndClassFeature],
        'subclassFeature': list[_DndSubclassFeature],
    },
)
