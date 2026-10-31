"""Read complete, explicitly selected library prompts via fy's pinned reader."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

READER = Path(__file__).with_name('fy_vendor') / 'style_library.py'
DEFAULT_ROOT = Path('.data/integrations/code-animation-341-styles')
STYLE_REPOSITORY = 'https://github.com/wqb578/code-animation-341-styles'
MAX_STYLE_CHARS = 24000


def style_command(command: str, root: Path, index: Path, *args: str) -> object:
    result = subprocess.run([sys.executable, str(READER), command, '--root', str(root), '--index', str(index), *args],
                            capture_output=True, encoding='utf-8', timeout=60, check=False)
    if result.returncode:
        raise ValueError('fy 画风检索失败：' + result.stderr.strip()[:1200])
    return json.loads(result.stdout)


def load_fy_style(style_id: str, root: str | Path | None = None, *, output: Path | None = None) -> dict:
    """Rebuild the index, then get one full prompt with SHA and dependency hints."""
    if not re.fullmatch(r'[A-Za-z]+\d+', style_id):
        raise ValueError('请用明确画风编号，如 X17 或 L36；不要用歧义名称')
    library = Path(root or os.environ.get('FY_STYLE_LIBRARY_ROOT') or DEFAULT_ROOT).resolve(strict=True)
    if not (library / '风格提示词').is_dir():
        raise ValueError('画风库根目录下必须有 风格提示词/')
    if output is not None and output.resolve().is_relative_to(library):
        raise ValueError('输出目录不能位于画风库内部')
    with tempfile.TemporaryDirectory(prefix='scid-fy-style-') as temp:
        index = Path(temp) / 'index.json'
        summary = style_command('build', library, index)
        rows = style_command('get', library, index, '--query', style_id.upper(), '--kind', 'prompt', '--audit-dependencies')
        selected = rows[0]
        if len(selected['content']) > MAX_STYLE_CHARS:
            raise ValueError('完整画风原文超过上下文预算；请另选画风，不截断原文')
        # Paths are useful locally but not needed in model prompts.
        for dependency in selected.get('dependency_candidates', []):
            dependency.pop('candidate_path', None)
        selected.update(repository=STYLE_REPOSITORY, attribution='来源：来自俯仰观照博主的动画画风整理',
                        prompt_count=summary['prompt_count'], library_commit=None, adaptation='independent_canvas_implementation')
        git_dir = library / '.git'
        if git_dir.exists():
            revision = subprocess.run(['git','-C',str(library),'rev-parse','HEAD'],capture_output=True,text=True,timeout=10,check=False)
            if revision.returncode == 0:
                selected['library_commit'] = revision.stdout.strip()
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
            (output / 'style-source.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2),encoding='utf-8')
            (output / 'style-index.json').write_bytes(index.read_bytes())
            license_dir = output / 'style-licenses'
            license_dir.mkdir(exist_ok=True)
            for name in ['LICENSE','NOTICE.md']:
                path = library / name
                if path.is_file(): (license_dir / name).write_bytes(path.read_bytes())
            third_party = library / '第三方许可'
            if third_party.is_dir():
                for path in third_party.glob('*_LICENSE.txt'):
                    (license_dir / path.name).write_bytes(path.read_bytes())
    return selected


def style_summary(style: dict) -> dict:
    return {key: style[key] for key in ['id','name','path','sha256','library_version','repository','library_commit',
                                        'attribution','prompt_count','adaptation','dependency_candidates','dependency_audit_complete']}
