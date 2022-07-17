"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import NotRequired

class _DndClassHD(TypedDict):
    number: int
    faces: int


_DndClassSkillsChoice = TypedDict(
    '_DndClassSkillsChoice',
    {
        'from': list[str],
        'count': int,
    },
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
