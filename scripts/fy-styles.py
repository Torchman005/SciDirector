"""List/search the external 341 style library or read one full style by ID."""
import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from scidirector_ai.chapter.fy import engine_info
from scidirector_ai.chapter.fy_styles import DEFAULT_ROOT, style_command


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['list','search','get'])
    parser.add_argument('--root',default=os.environ.get('FY_STYLE_LIBRARY_ROOT') or str(DEFAULT_ROOT))
    parser.add_argument('--query',default='')
    parser.add_argument('--limit',type=int,default=8)
    args=parser.parse_args()
    engine_info()
    root=Path(args.root).resolve(strict=True)
    with TemporaryDirectory(prefix='scid-fy-style-') as directory:
        index=Path(directory)/'index.json'
        style_command('build',root,index)
        extra=['--kind','prompt','--query',args.query,'--limit',str(args.limit)]
        if args.command=='get': extra.append('--audit-dependencies')
        print(json.dumps(style_command(args.command,root,index,*extra),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
