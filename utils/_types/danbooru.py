"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from typing import TypedDict


__all__ = ('DanbooruPayload',)


class DanbooruPayload(TypedDict):
    id: int
    created_at: str
    uploader_id: int
    score: int
    source: str
    md5: str
    last_comment_bumped_at: str | None
    rating: str
    image_width: int
    image_height: int
    tag_string: str
    fav_count: int
    file_ext: str
    last_noted_at: str | None
    parent_id: int | None
    has_children: bool
    approver_id: int | None
    tag_count_general: int
    tag_count_artist: int
    tag_count_character: int
    tag_count_copyright: int
    file_size: int
    up_score: int
    down_score: int
    is_pending: bool
    is_flagged: bool
    is_deleted: bool
    tag_count: int
    updated_at: str
    is_banned: bool
    pixiv_id: str | None
    last_commented_at: str | None
    has_active_children: bool
    bit_flags: int
    tag_count_meta: int
    has_large: bool
    has_visible_children: bool
    tag_string_general: str
    tag_string_character: str
    tag_string_copyright: str
    tag_string_artist: str
    tag_string_meta: str
    file_url: str
    large_file_url: str
    preview_file_url: str
