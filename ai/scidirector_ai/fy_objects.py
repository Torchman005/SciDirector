"""Editable geometry for single-shot fy drawings, independent of their artwork."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .scene import Box, SceneModel

ObjectId = Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')]
Port = Literal['auto', 'left', 'right', 'top', 'bottom']
FY_REVISION_PREFIX = 'SCID_FY_REVISION_V1:'


class FyFrame(SceneModel):
    time: float = Field(ge=0, le=1)
    dx: float = Field(default=0, ge=-1, le=1)
    dy: float = Field(default=0, ge=-1, le=1)
    opacity: float = Field(default=1, ge=0, le=1)
    scale: float = Field(default=1, ge=.25, le=4)
    easing: Literal['linear', 'smooth'] = 'smooth'


class FyObject(SceneModel):
    id: ObjectId
    name: str = Field(default='', max_length=160)
    box: Box
    layer: int = Field(default=0, ge=-100, le=100)
    overlap_group: str = Field(default='', max_length=64)
    keyframes: list[FyFrame] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def ordered(self) -> FyObject:
        times = [k.time for k in self.keyframes]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError('对象关键帧 time 必须严格递增')
        return self


class FyRelation(SceneModel):
    id: ObjectId
    source: ObjectId
    target: ObjectId
    source_port: Port = 'auto'
    target_port: Port = 'auto'
    color: Literal['ink', 'mute', 'line', 'q', 'k', 'v', 'gold', 'blue'] = 'ink'
    stroke_width: float = Field(default=3, ge=1, le=12)
    arrow: bool = True
    flow: bool = False


class FyDocument(SceneModel):
    version: Literal[1] = 1
    objects: list[FyObject] = Field(min_length=1, max_length=32)
    relations: list[FyRelation] = Field(default_factory=list, max_length=48)

    @model_validator(mode='after')
    def references(self) -> FyDocument:
        ids = [o.id for o in self.objects] + [r.id for r in self.relations]
        if len(ids) != len(set(ids)):
            raise ValueError('对象和关系的 id 必须全局唯一')
        objects = {o.id for o in self.objects}
        for relation in self.relations:
            if relation.source not in objects or relation.target not in objects:
                raise ValueError(f'关系 {relation.id} 引用了不存在的对象')
            if relation.source == relation.target:
                raise ValueError(f'关系 {relation.id} 不能连接同一个对象')
        return self


class FyObjectPatch(SceneModel):
    id: ObjectId
    box: Box | None = None
    layer: int | None = Field(default=None, ge=-100, le=100)
    overlap_group: str | None = Field(default=None, max_length=64)
    keyframes: list[FyFrame] | None = Field(default=None, max_length=32)

    @model_validator(mode='after')
    def nonempty(self) -> FyObjectPatch:
        if not self.model_dump(exclude={'id'}, exclude_none=True):
            raise ValueError('对象补丁必须包含实际修改字段')
        return self


class FyRevision(SceneModel):
    expect_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    update_objects: list[FyObjectPatch] = Field(default_factory=list, max_length=32)
    upsert_relations: list[FyRelation] = Field(default_factory=list, max_length=48)
    remove_relations: list[ObjectId] = Field(default_factory=list, max_length=48)
    explanation: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def distinct(self) -> FyRevision:
        for ids in ([p.id for p in self.update_objects], [r.id for r in self.upsert_relations], self.remove_relations):
            if len(ids) != len(set(ids)):
                raise ValueError('同一修订不能重复指定 id')
        if {r.id for r in self.upsert_relations} & set(self.remove_relations):
            raise ValueError('不能同时替换和删除同一个关系')
        return self


def parse_fy_revision(comment: str) -> FyRevision | None:
    if not comment.startswith(FY_REVISION_PREFIX):
        return None
    return FyRevision.model_validate_json(comment[len(FY_REVISION_PREFIX):])


def revision_hash(context: dict, code: str) -> str:
    content = json.dumps({'context': context, 'code': code}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class FySnapshot:
    document: FyDocument
    revision: str
    context: dict
    code: str


def extract_fy_snapshot(html: str) -> FySnapshot | None:
    from .fy_generation import extract_fy_code, extract_fy_palette

    match = re.search(r'<script id="scid-fy-data" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        return None
    context = json.loads(match[1])
    if 'object_document' not in context:
        return None
    revision = context.pop('revision', '')
    context.pop('parent_revision', None)
    context.pop('changes', None)
    document = FyDocument.model_validate(context['object_document'])
    code = extract_fy_code(html)
    if revision != revision_hash(context, code):
        raise ValueError('fy 对象快照与版本哈希不一致，不能应用精确修订')
    if extract_fy_palette(html) != context.get('palette'):
        raise ValueError('fy 配色来源与对象快照不一致，不能应用精确修订')
    return FySnapshot(document, revision, context, code)


def document_changes(before: FyDocument, after: FyDocument) -> list[dict]:
    changes = []
    for kind in ('objects', 'relations'):
        old = {o.id: o.model_dump() for o in getattr(before, kind)}
        new = {o.id: o.model_dump() for o in getattr(after, kind)}
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) != new.get(key):
                changes.append({'kind': kind, 'id': key, 'before': old.get(key), 'after': new.get(key)})
    return changes


def apply_fy_revision(previous: FySnapshot, revision: FyRevision) -> FyDocument:
    if revision.expect_revision != previous.revision:
        raise ValueError('fy 版本冲突：请基于当前镜头重新提交修改')
    objects = {o.id for o in previous.document.objects}
    unknown = {p.id for p in revision.update_objects} - objects
    if unknown:
        raise ValueError('不能修改不存在的对象：' + ', '.join(sorted(unknown)))
    relations = {r.id for r in previous.document.relations}
    if unknown := set(revision.remove_relations) - relations:
        raise ValueError('不能删除不存在的关系：' + ', '.join(sorted(unknown)))
    patches = {p.id: p.model_dump(exclude={'id'}, exclude_none=True) for p in revision.update_objects}
    replacements = {r.id: r.model_dump() for r in revision.upsert_relations}
    result = FyDocument.model_validate({
        **previous.document.model_dump(),
        'objects': [{**o.model_dump(), **patches.get(o.id, {})} for o in previous.document.objects],
        'relations': [replacements.get(r.id, r.model_dump()) for r in previous.document.relations
                      if r.id not in revision.remove_relations]
                     + [r.model_dump() for r in revision.upsert_relations if r.id not in relations],
    })
    if not document_changes(previous.document, result):
        raise ValueError('fy 修订没有实际变化；只改 explanation 不算修订')
    return result
