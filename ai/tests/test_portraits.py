import array
import json
import math
import os
import shutil
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from scidirector_ai.config import Settings
from scidirector_ai.portraits import load_portrait, portrait_timeline, STATES
from scidirector_ai.presenter import render_presenter
from scidirector_ai.sandbox.runner import SandboxRunner


def pack(tmp_path, mouths=True):
    states={}
    # These geometric test fixtures validate alpha and switching, not character art.
    for state,color in zip(sorted(STATES),['red','green','blue','yellow']):
        picture=Image.new('RGBA',(128,192))
        ImageDraw.Draw(picture).rectangle((24,16,104,184),fill=color)
        picture.save(tmp_path/f'{state}.png')
        states[state]={'image':f'{state}.png'}
        if mouths:
            ImageDraw.Draw(picture).ellipse((54,60,74,84),fill='black')
            picture.save(tmp_path/f'{state}.open.png');states[state]['mouth_open']=f'{state}.open.png'
    manifest={'version':1,'states':states,'cues':[{'time':0,'state':'neutral'},{'time':.3,'state':'think'},{'time':.6,'state':'surprised'}]}
    file=tmp_path/'model.portrait.json';file.write_text(json.dumps(manifest))
    return file


def test_manifest_validation_and_explicit_states(tmp_path):
    file=pack(tmp_path);manifest,files=load_portrait(file)
    assert len(files)==8
    assert portrait_timeline(manifest,[],50,10)==[{'time':0,'state':'neutral'},{'time':3,'state':'think'},{'time':6,'state':'surprised'}]
    raw=json.loads(file.read_text());raw['states']['neutral']['image']='../outside.png';file.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='路径'):load_portrait(file)


def test_speech_pose_holds_through_short_gaps(tmp_path):
    file=pack(tmp_path,mouths=False);manifest,_=load_portrait(file);manifest.cues=[]
    timeline=portrait_timeline(manifest,[0]*25+[1]*10+[0]*5+[1]*10+[0]*50,50,2)
    assert [c['state'] for c in timeline]==['neutral','explain','neutral']
    assert timeline[1]['time']==.5 and timeline[-1]['time']>1.3


@pytest.mark.skipif(not os.environ.get('SCID_CHROME') or not shutil.which('ffmpeg'),reason='需要 Chromium / FFmpeg')
@pytest.mark.parametrize('mouths',[True,False])
def test_real_alpha_render_without_cubism_and_seek_determinism(tmp_path,mouths):
    model=pack(tmp_path,mouths)
    audio=tmp_path/'speech.wav'
    samples=array.array('h',[int(9000*math.sin(i*.12)) if 4000<i<16000 else 0 for i in range(24000)])
    with wave.open(str(audio),'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(samples.tobytes())
    result=render_presenter(Settings(live2d_core_path='',live2d_timeout_sec=90),SandboxRunner(),
        model_path=str(model),audio_path=str(audio),output_dir=str(tmp_path),duration_sec=1.5,width=128,height=192,fps=8)
    assert Path(result['video_path']).stat().st_size>100
    assert result['lip_sync']==mouths
    assert Path(result['envelope_path']).is_file()


def test_mouth_parameter_autodiscovery():
    import subprocess
    if not shutil.which('node'):pytest.skip('需要 Node')
    runtime=Path(__file__).parents[1]/'scidirector_ai/presenter_runtime.js'
    code='const window={};'+runtime.read_text(encoding='utf-8')+'''
const assert=require('node:assert/strict');
assert.deepEqual(resolveMouthParameters(new Set(['PARAM_MOUTH_OPEN_Y']),[],''),['PARAM_MOUTH_OPEN_Y']);
assert.deepEqual(resolveMouthParameters(new Set(['MouthOpen']),[{Name:'LipSync',Ids:['missing']}],''),['MouthOpen']);
assert.throws(()=>resolveMouthParameters(new Set(['MouthOpenA','MouthOpenB']),[],''));
assert.throws(()=>resolveMouthParameters(new Set(['MouthOpen']),[],'wrong'));
'''
    result=subprocess.run(['node','-e',code],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
