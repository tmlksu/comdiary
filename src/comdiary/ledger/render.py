from __future__ import annotations

import json
import os
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from ..models import Meeting, Project, Segment

#: Maps person id -> display name. The LLM is told to use registry ids, which
#: keeps the canonical JSON stable, but "suzuki" reads badly in a document a
#: human is meant to open — so resolve to the real name at render time.
Names = dict[str, str]


def _quote(value) -> str:
    """YAML-safe scalar for frontmatter."""
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)


def _who(value, names: Names | None = None) -> str:
    if not value:
        return ""
    return (names or {}).get(value, value)


def _whos(values, names: Names | None = None) -> str:
    return "、".join(_who(v, names) for v in (values or []))


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("comdiary", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["q"] = _quote
    env.filters["who"] = _who
    env.filters["whos"] = _whos
    return env


ENV = _env()


def _tidy(text: str) -> str:
    """Collapse the blank-line noise that conditional template blocks leave behind."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                out.append(line)
    return "\n".join(out).strip("\n") + "\n"


def relpath(target: Path, start: Path) -> str:
    """POSIX-style relative link, correct on Windows too."""
    return Path(os.path.relpath(target, start)).as_posix()


def render_meeting(meeting: Meeting, names: Names | None = None) -> str:
    return _tidy(ENV.get_template("meeting.md.j2").render(m=meeting, names=names or {}))


def render_segment_note(
    meeting: Meeting, segment: Segment, project_id: str, meeting_rel: str, names: Names | None = None
) -> str:
    return _tidy(
        ENV.get_template("segment_note.md.j2").render(
            m=meeting, s=segment, project_id=project_id, meeting_rel=meeting_rel, names=names or {}
        )
    )


def render_inbox_segment(
    meeting: Meeting, segment: Segment, meeting_rel: str, names: Names | None = None
) -> str:
    return _tidy(
        ENV.get_template("inbox_segment.md.j2").render(
            m=meeting, s=segment, meeting_rel=meeting_rel, names=names or {}
        )
    )


def render_log_entry(
    meeting: Meeting,
    segment: Segment,
    note_rel: str,
    meeting_rel: str,
    names: Names | None = None,
) -> str:
    return _tidy(
        ENV.get_template("log_entry.md.j2").render(
            m=meeting, s=segment, note_rel=note_rel, meeting_rel=meeting_rel, names=names or {}
        )
    )


def render_open_questions(
    meeting: Meeting, questions: list, meeting_rel: str, names: Names | None = None
) -> str:
    return _tidy(
        ENV.get_template("open_questions_entry.md.j2").render(
            m=meeting, questions=questions, meeting_rel=meeting_rel, names=names or {}
        )
    )


def render_project_readme(project: Project) -> str:
    return _tidy(ENV.get_template("project_readme.md.j2").render(p=project))
