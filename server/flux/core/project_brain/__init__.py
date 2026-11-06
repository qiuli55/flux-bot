"""Project Brain（主规格 §5.6；实施计划 ⑪）。"""

from flux.core.project_brain.repository import ProjectBrainRepository
from flux.core.project_brain.service import (
    CONTEXT_MAX_ENTRIES,
    SECTION_TITLES,
    ProjectBrain,
    ScanOutcome,
    render_overview,
    render_tech_stack,
)

__all__ = [
    "CONTEXT_MAX_ENTRIES",
    "SECTION_TITLES",
    "ProjectBrain",
    "ProjectBrainRepository",
    "ScanOutcome",
    "render_overview",
    "render_tech_stack",
]
