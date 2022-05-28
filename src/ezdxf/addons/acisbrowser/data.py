#  Copyright (c) 2022, Manfred Moitzi
#  License: MIT License
from typing import List, Iterator

from ezdxf.acis.sab import parse_sab, SabEntity


class AcisData:
    def __init__(self, name: str = "unknown", handle: str = ""):
        self.lines: List[str] = []
        self.name: str = name
        self.handle: str = handle


class BinaryAcisData(AcisData):
    def __init__(self, data: bytes, name: str, handle: str):
        super().__init__(name, handle)
        self.lines = list(make_sab_records(data))


class TextAcisData(AcisData):
    def __init__(self, data: List[str], name: str, handle: str):
        super().__init__(name, handle)
        self.lines = list(data)


def make_sab_records(data: bytes) -> Iterator[str]:
    builder = parse_sab(data)
    yield from builder.header.dumps()
    builder.reset_ids()
    for entity in builder.entities:
        content = [str(entity)]
        if not entity.attributes.is_null_ptr:
            content.append(str(entity.attributes))
        for tag in entity.data:
            if isinstance(tag.value, SabEntity):
                if not tag.value.is_null_ptr:
                    content.append(str(tag.value))
            else:
                content.append(f"{tag.value}<{tag.tag}>")
        yield " ".join(content)