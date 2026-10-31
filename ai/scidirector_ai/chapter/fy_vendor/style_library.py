"""Portable, read-only retrieval of complete style prompts. Python standard library only.

build indexes originals (it does not copy them); get returns their full verified text.
list shows the catalog (category, one-line look, fit, keywords) for choosing candidates.
Search is lexical candidate retrieval, never an aesthetic or semantic verdict.

Library root resolution: --root, then env FY_STYLE_LIBRARY_ROOT, then "root" in
adapters/style-library.config.json (relative paths are resolved against the skill root).
Index defaults to adapters/style-index.json under the skill root.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PureWindowsPath

if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
ID = re.compile(r'^([A-Z]+\d+)(?:[_\s]|$)', re.I)
SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = SKILL_ROOT/'adapters'/'style-index.json'
CONFIG_FILE = SKILL_ROOT/'adapters'/'style-library.config.json'
META_KEYS = {'分类':'category_label','长什么样':'look','适合':'fit','关键词':'keywords','风格':'style'}

def resolve_root(cli_root):
    if cli_root: return cli_root, 'cli'
    env = os.environ.get('FY_STYLE_LIBRARY_ROOT')
    if env: return env, 'env:FY_STYLE_LIBRARY_ROOT'
    if CONFIG_FILE.is_file():
        cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
        root = cfg.get('root')
        if root:
            p = Path(root)
            if not p.is_absolute(): p = SKILL_ROOT/p
            return str(p), 'config:'+CONFIG_FILE.name
    raise ValueError('MISSING_LIBRARY_ROOT: pass --root, set FY_STYLE_LIBRARY_ROOT, or set "root" in '+str(CONFIG_FILE))

def category_of(row):
    parts = row['path'].split('/')
    return parts[1] if row['kind']=='prompt' and len(parts)>2 else '示例'

def metadata(text):
    out = {}
    for line in text.splitlines()[:20]:
        m = re.match(r'^\s*-\s*([^：:]+)[：:]\s*(.+)$', line)
        if m and m[1].strip() in META_KEYS: out[META_KEYS[m[1].strip()]] = m[2].strip()
    return out

def digest(raw): return hashlib.sha256(raw).hexdigest()

def index_library(root):
    root=Path(root).resolve()
    prompt_root=root/'风格提示词'
    if not prompt_root.is_dir(): raise ValueError('MISSING_PROMPT_DIRECTORY: '+str(prompt_root))
    rows=[]
    sources=[('prompt',prompt_root.rglob('*.md')),('example',(root/'示例').rglob('提示词.md'))]
    for kind,paths in sources:
        for path in sorted(paths):
            label=path.stem if kind=='prompt' else path.parent.name
            match=ID.match(label)
            if not match: raise ValueError('UNRECOGNIZED_STYLE_ID: '+str(path))
            raw=path.read_bytes(); text=raw.decode('utf-8-sig')
            rows.append({'id':match[1].upper(),'name':label[match.end():], 'kind':kind,
                         'path':path.relative_to(root).as_posix(),'sha256':digest(raw),
                         'bytes':len(raw),'lines':len(text.splitlines())})
    ids=[r['id'] for r in rows if r['kind']=='prompt']
    if len(set(ids))!=len(ids): raise ValueError('DUPLICATE_FORMAL_ID')
    version=digest('\n'.join(r['path']+'\t'+r['sha256'] for r in rows).encode('utf-8'))
    return {'schema_version':1,'library_version':version,'prompt_count':len(ids),'entries':rows,
            'content_included':False,'search_is_semantic_judgment':False}

def safe_file(root, rel):
    p=(Path(root).resolve()/rel).resolve()
    if not p.is_relative_to(Path(root).resolve()): raise ValueError('PATH_OUTSIDE_LIBRARY')
    if not p.is_file(): raise FileNotFoundError('MISSING_STYLE_FILE: '+str(p))
    return p

def read_row(root,row):
    p=safe_file(root,row['path']); raw=p.read_bytes()
    if digest(raw)!=row['sha256']: raise ValueError('STYLE_VERSION_CHANGED: '+row['path'])
    return raw.decode('utf-8-sig')

def select(index, query, kind=None):
    rows=[r for r in index['entries'] if kind is None or r['kind']==kind]
    exact=[r for r in rows if r['id']==query.upper() or r['path']==query or r['name']==query]
    if not exact: exact=[r for r in rows if query.casefold() in r['name'].casefold()]
    if not exact: raise ValueError('STYLE_NOT_FOUND: '+query)
    if len(exact)!=1:
        raise ValueError('AMBIGUOUS_STYLE: '+json.dumps([{k:r[k] for k in ['id','kind','path']} for r in exact],ensure_ascii=False))
    return exact[0]

def dependency_candidates(root,row,text):
    # Deliberately conservative hints. Paths in prose / dynamic imports need human review.
    found=re.findall(r'`([^`\n]+\.(?:html|md|json|js|ts|tsx|woff2?|ttf|otf|svg|png|jpg|mp[34]|wav))`',text)
    out=[]
    for token in dict.fromkeys(found):
        absolute=PureWindowsPath(token).is_absolute() or Path(token).is_absolute()
        if token.startswith(('http:','https:')):
            out.append({'reference':token,'status':'external_not_requested'}); continue
        p=Path(token) if absolute else Path(root)/Path(row['path']).parent/token
        out.append({'reference':token,'status':'exists' if p.is_file() else 'not_found_at_literal_path',
                    'absolute_dependency':absolute,'candidate_path':str(p)})
    return out

def search(root,index,query):
    words=[w.casefold() for w in re.split(r'\s+',query.strip()) if w]
    hits=[]
    for r in index['entries']:
        content=read_row(root,r).casefold(); name=(r['id']+' '+r['name']).casefold()
        score=sum((6 if w in name else 1 if w in content else 0) for w in words)
        if score: hits.append({**r,'lexical_score':score})
    return sorted(hits,key=lambda r:(-r['lexical_score'],r['path']))

def catalog(root,index,category=None,kind=None):
    rows=[]
    for r in index['entries']:
        if kind and r['kind']!=kind: continue
        cat=category_of(r)
        if category and category.casefold() not in cat.casefold(): continue
        rows.append({'id':r['id'],'name':r['name'],'kind':r['kind'],'category':cat,'path':r['path'],
                     **metadata(read_row(root,r))})
    return rows

def selection_queries(content,names):
    queries=list(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9])([A-Za-z]+\d+)(?![A-Za-z0-9])',content)))
    if names:
        for line in content.splitlines():
            line=line.strip().lstrip('-*0123456789.、) ').strip()
            if line and not line.startswith('#') and not re.search(r'(?<![A-Za-z0-9])[A-Za-z]+\d+(?![A-Za-z0-9])',line):
                queries.append(line)
    return list(dict.fromkeys(queries))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['build','list','search','get','selection'])
    ap.add_argument('--root',help='Library root containing 风格提示词 and optional 示例 (see resolution order above)')
    ap.add_argument('--index',default=str(DEFAULT_INDEX))
    ap.add_argument('--query',default='')
    ap.add_argument('--kind',choices=['prompt','example'])
    ap.add_argument('--category',help='list: substring of the category folder, e.g. L_ or 卡通')
    ap.add_argument('--limit',type=int,default=12)
    ap.add_argument('--selection-file')
    ap.add_argument('--names',action='store_true',help='selection: also treat lines without an ID as exact/contained names')
    ap.add_argument('--audit-dependencies',action='store_true')
    a=ap.parse_args()
    try:
        a.root,root_source=resolve_root(a.root)
        if not Path(a.root).is_dir(): raise ValueError('LIBRARY_ROOT_NOT_FOUND ('+root_source+'): '+a.root)
        if a.command=='build':
            result=index_library(a.root)
            Path(a.index).parent.mkdir(parents=True,exist_ok=True)
            Path(a.index).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({k:result[k] for k in ['library_version','prompt_count','content_included']},ensure_ascii=False)); return
        index=json.loads(Path(a.index).read_text(encoding='utf-8-sig'))
        if a.command=='list':
            print(json.dumps(catalog(a.root,index,a.category,a.kind),ensure_ascii=False,indent=2)); return
        if a.command=='search':
            print(json.dumps(search(a.root,index,a.query)[:a.limit],ensure_ascii=False,indent=2)); return
        if a.command=='selection':
            if not a.selection_file: raise ValueError('MISSING_SELECTION_FILE')
            content=Path(a.selection_file).read_text(encoding='utf-8-sig')
            queries=selection_queries(content,a.names)
            if not queries: raise ValueError('NO_STYLE_IDS_IN_SELECTION')
            # Resolve ALL selections before emitting any partial success.
            selected=[select(index,q,a.kind) for q in queries]
        else: selected=[select(index,a.query,a.kind)]
        results=[]
        for row in selected:
            content=read_row(a.root,row)
            result={**row,'library_version':index['library_version'],'content':content}
            if a.audit_dependencies:
                result['dependency_candidates']=dependency_candidates(a.root,row,content)
                result['dependency_audit_complete']=False
            results.append(result)
        print(json.dumps(results,ensure_ascii=False,indent=2))
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as e:
        print(str(e),file=sys.stderr); raise SystemExit(2)

if __name__=='__main__': main()
