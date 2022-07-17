"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from typing import TypedDict


__all__ = ('GelbooruPayload',)


class GelbooruPostPayload(TypedDict):
    id: int
    created_at: str
    score: int
    width: int
    height: int
    md5: str
    directory: str
    image: str
    rating: str
    source: str
    change: int
    owner: str
    creator_id: int
    parent_id: int
    sample: int
    preview_height: int
    preview_width: int
    tags: str
    title: str
    has_notes: str
    has_comments: str
    file_url: str
    preview_url: str
    sample_url: str
    sample_height: int
    sample_width: int
    status: str
    post_locked: int
    has_children: str


class GelbooruPagination(TypedDict):
    limit: int
    offset: int
    count: int


GelbooruPayload = TypedDict('GelbooruPayload', {'@attributes': GelbooruPagination, 'post': list[GelbooruPostPayload]})
