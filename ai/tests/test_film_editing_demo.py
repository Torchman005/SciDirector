"""Static controls for the hand-authored motion target; visual approval is separate."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest


DEMO = Path(__file__).parents[2] / "demos" / "film-editing.html"


class DemoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.external: list[str] = []
        self.scripts: list[str] = []
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        for key in ("src", "href"):
            if values.get(key) and not values[key].startswith("#"):
                self.external.append(values[key])
        self.in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.scripts.append(data)


def test_film_editing_demo_is_offline_and_seek_driven() -> None:
    parser = DemoParser()
    parser.feed(DEMO.read_text(encoding="utf-8"))
    assert len(parser.ids) == len(set(parser.ids))
    assert not parser.external
    assert len(parser.scripts) == 1
    script = parser.scripts[0]
    assert "window.__seek = seconds =>" in script
    assert "window.__seek(0);" in script
    assert "window.__ready = true;" in script
    assert "piece-${index + 1}" in script
    assert "requestAnimationFrame(tick)" in script  # Preview drives the absolute-time function.
    if node := shutil.which("node"):
        result = subprocess.run([node, "--check"], input=script, capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        assert result.returncode == 0, result.stderr


def test_film_editing_timeline_is_reversible_without_a_browser() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js 未安装")
    parser = DemoParser()
    parser.feed(DEMO.read_text(encoding="utf-8"))
    harness = r"""
      const fs=require('fs'),vm=require('vm'),assert=require('assert');
      const source=fs.readFileSync(0,'utf8'),nodes=new Map();
      class Element {
        constructor(id=''){this.id=id;this.style={};this.attributes={};this.children=[];this.value='';}
        setAttribute(key,value){this.attributes[key]=String(value);if(key==='id'){this.id=String(value);nodes.set(this.id,this);}}
        appendChild(child){this.children.push(child);return child;}
        addEventListener(){}
        getTotalLength(){return 600;}
      }
      for(const id of ['film-layer','cut-guide-one','cut-guide-two','cut-sparks-one','cut-sparks-two',
        'scissors','blade-upper','blade-lower','label-one','label-two','label-three','closing',
        'underline','scrub','clock','play'])nodes.set(id,new Element(id));
      const document={getElementById:id=>nodes.get(id),createElementNS:()=>new Element(),
        addEventListener(){},body:{classList:{add(){}}}};
      const window={};
      vm.runInNewContext(source,{document,window,location:{search:''},URLSearchParams,
        performance:{now:()=>0},requestAnimationFrame:()=>0,cancelAnimationFrame(){}},
        {timeout:1000});
      assert.strictEqual(window.__ready,true);
      const snapshot=t=>{
        window.__seek(t);
        return JSON.stringify(['piece-1','piece-2','piece-3','scissors','blade-upper',
          'blade-lower','label-one','label-two','label-three','closing','underline']
          .map(id=>({id,attrs:nodes.get(id).attributes,style:nodes.get(id).style})));
      };
      const early=snapshot(2.5),late=snapshot(9);
      assert.notStrictEqual(early,late);
      assert.strictEqual(snapshot(2.5),early);
      assert.strictEqual(snapshot(9),late);
      assert.strictEqual(window.__seek(-1),0);
      assert.strictEqual(window.__seek(99),10);
      const bounds=id=>{
        const match=/translate\(([-\d.]+) ([-\d.]+)\) rotate\(([-\d.]+)/.exec(nodes.get(id).attributes.transform);
        assert(match);
        const x=Number(match[1]),y=Number(match[2]),angle=Number(match[3])*Math.PI/180;
        const halfWidth=210*Math.abs(Math.cos(angle))+105*Math.abs(Math.sin(angle));
        const halfHeight=210*Math.abs(Math.sin(angle))+105*Math.abs(Math.cos(angle));
        return {left:x+210-halfWidth,right:x+210+halfWidth,
          top:y+105-halfHeight,bottom:y+105+halfHeight};
      };
      window.__seek(9);
      const a=bounds('piece-1'),b=bounds('piece-2'),c=bounds('piece-3');
      assert(a.right+10<b.left && b.right+10<c.left,'收尾胶片段间距不足');
      for(let i=0;i<=200;i++){
        window.__seek(i/20);
        const piece=nodes.get('piece-3');
        if(Number(piece.style.opacity)<=.35)continue;
        const box=bounds('piece-3');
        const right=box.right+20,bottom=box.bottom+20;
        assert(!(right>1248&&bottom>495),`胶片进入讲解员区域：${i/20}s`);
      }
    """
    result = subprocess.run([node, "-e", harness], input=parser.scripts[0], capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
