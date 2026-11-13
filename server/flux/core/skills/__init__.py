"""技能库：技能包格式、持久化、Bot 分配与上下文注入。"""

from flux.core.skills.package import (
    MAX_BODY_CHARS,
    SOURCE,
    SkillPackageDocument,
    build_package,
    parse_package_document,
)
from flux.core.skills.repository import SkillLibraryRepository
from flux.core.skills.service import SKILLS_BLOCK_MAX_CHARS, SkillLibraryService

__all__ = [
    "MAX_BODY_CHARS",
    "SKILLS_BLOCK_MAX_CHARS",
    "SOURCE",
    "SkillLibraryRepository",
    "SkillLibraryService",
    "SkillPackageDocument",
    "build_package",
    "parse_package_document",
]
