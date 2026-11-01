"""Restore a consumer's lost plan without replanning or inventing shot results."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from scidirector_ai.graph.state import PipelineState, make_event
from scidirector_ai.pbconv import shots_payload_json
from scidirector_ai.schemas import (
    CriticFeedback, NarrationRevision, RenderArtifact, RenderEngine, RepairTask,
    SceneTag, ShotSpec,
)
from test_pipeline_resume import _request, _run, _runner, _values


def recovery_checkpoint():
    """Synthetic history shared with the Go persistence contract tests."""
    shots = [ShotSpec(shot_id=f'job-x-s{i:03d}', index=i, narration='original',
                      visual_brief='diagram', tag=tag, engine=RenderEngine.MOTION)
             for i, tag in enumerate([SceneTag.MATH, SceneTag.DATA, SceneTag.AMBIENCE])]
    original_plan = shots_payload_json(shots, 'outline')
    revision = NarrationRevision(before='original', after='corrected', reason='verified',
                                 task_ids=['r1'], attempt=2)
    corrected = shots[0].model_copy(update={'narration': 'corrected', 'narration_revisions': [revision]})
    task = RepairTask(task_id='r1', target='label', evidence='overlap',
                      instruction='move label', acceptance='labels separate')
    rejected = CriticFeedback(kind='content_review', score=.4, suggestions=['move label'],
                              repair_tasks=[task], artifact_id='v1', attempt=1)
    approved = CriticFeedback(passed=True, kind='content_review', score=.9, attempt=2,
                             artifact_id='v2', repair_tasks=[task.model_copy(update={
                                 'status': 'resolved', 'resolution_evidence': 'current frame: labels separate'})])
    artifacts = [RenderArtifact(artifact_id=f'v{i}', shot_id=shots[0].shot_id,
                               video_path=f'video-{i}.mp4', audio_path=f'audio-{i}.mp3',
                               attempt=i, rendered_at_unix_ms=1700000000000 + i)
                 for i in [1, 2]]
    def event(node, shot=shots[0], **kw):
        return dict(job_id='job-x', shot_id=shot.shot_id, node=node,
                    ts_unix_ms=1700000000000, **kw)
    def code(shot, attempt, source):
        return event('code', shot, attempt=attempt, status='GENERATING', payload_json=json.dumps({
            'patch': {'shot_id': shot.shot_id, 'code': source, 'language': 'html+js'}}))
    history = [dict(job_id='job-x', node='plan', payload_json=original_plan),
               code(shots[0], 1, 'old source'),
               event('render', attempt=1, status='CRITIQUING', artifact=artifacts[0]),
               event('critique', attempt=1, status='REJECTED', feedback=rejected),
               event('revise', attempt=1, status='RETRYING'),
               code(shots[0], 2, 'latest source'),
               event('render', attempt=2, status='CRITIQUING', artifact=artifacts[1], narration_patch=corrected),
               event('critique', attempt=2, status='APPROVED', feedback=approved, artifact=artifacts[1]),
               code(shots[1], 1, 'second source'),
               event('render', shots[1], attempt=1, status='FAILED', error='layout failed'),
               event('revise', shots[1], attempt=1, status='AWAITING_HUMAN')]
    return dict(job_id='job-x', shots=[corrected, *shots[1:]], outline='outline', cursor=2,
                finished=False, events=history, attempts={shots[0].shot_id: 2, shots[1].shot_id: 1})


def test_recovery_wire_contract_and_history_order(tmp_path):
    state = recovery_checkpoint()
    runner, app = _runner(tmp_path, state)
    events = _run(runner, _request(resume=True))
    assert app.inputs == [None]
    event = events[0]
    assert event['node'] == 'plan' and event['shot_id'] == '' and event['status'] == ''
    payload = json.loads(event['payload_json'])
    # Start from original text; typed render patches commit revised text + media together.
    assert payload['shots'][0]['narration'] == 'original'
    assert [s['engine'] for s in payload['shots']] == [6, 6, 6]
    replay = payload['recovery_events']
    assert [e['node'] for e in replay] == [
        'render', 'critique', 'revise', 'code', 'render', 'critique', 'code', 'render', 'revise']
    assert 'old source' not in event['payload_json']
    assert replay[0]['ts_unix_ms'] == '1700000000000'  # protobuf int64 JSON is a string
    assert replay[4]['narration_patch']['narration'] == 'corrected'
    golden = Path(__file__).resolve().parents[2] / 'proto/testdata/pipeline_plan_recovery.json'
    assert payload == json.loads(golden.read_text(encoding='utf-8'))
    assert state['events'][2]['artifact'].artifact_id == 'v1'  # snapshot remains unchanged


def test_dictionary_checkpoint_roundtrip(tmp_path):
    state = recovery_checkpoint()
    runner, _ = _runner(tmp_path, state)
    expected = runner._recovery_plan_event('job-x', {})
    restored = copy.deepcopy(state)
    restored['shots'] = [s.model_dump(mode='json') for s in restored['shots']]
    for event in restored['events']:
        for key in ['artifact', 'feedback', 'narration_patch']:
            if event.get(key) is not None:
                event[key] = event[key].model_dump(mode='json')
    restored_runner, _ = _runner(tmp_path, restored)
    actual = restored_runner._recovery_plan_event('job-x', {})
    assert actual['payload_json'] == expected['payload_json']


def test_finished_checkpoint_still_restores_baseline(tmp_path):
    state = recovery_checkpoint()
    state['finished'] = True
    runner, app = _runner(tmp_path, state)
    events = _run(runner, _request(resume=True))
    assert app.inputs == []
    assert [e['node'] for e in events] == ['plan', 'pipeline']
    assert len(json.loads(events[0]['payload_json'])['shots']) == 3


@pytest.mark.parametrize('damage', ['job', 'history_job', 'mismatched_plan', 'invalid_model'])
def test_invalid_recovery_stops_before_continuation(tmp_path, damage):
    state = recovery_checkpoint()
    if damage == 'job':
        state['job_id'] = 'other-job'
    elif damage == 'history_job':
        state['events'][2]['job_id'] = 'other-job'
    elif damage == 'mismatched_plan':
        payload = json.loads(state['events'][0]['payload_json'])
        payload['shots'][0]['shot_id'] = 'other-shot'
        state['events'][0]['payload_json'] = json.dumps(payload)
    else:
        state['shots'][0] = {'shot_id': 's', 'duration_sec': -1}
    runner, app = _runner(tmp_path, state)
    events = _run(runner, _request(resume=True))
    assert app.inputs == []
    assert len(events) == 1 and events[0]['status'] == 'FAILED'
    assert '恢复分镜表失败' in events[0]['error']


def test_interrupted_before_director_has_no_recovery_payload(tmp_path):
    runner, app = _runner(tmp_path, {'job_id': 'job-x', 'shots': [], 'finished': False})
    events = _run(runner, _request(resume=True))
    assert app.inputs == [None]
    assert all(e['node'] != 'plan' for e in events)


def test_legacy_checkpoint_without_plan_event_uses_shots(tmp_path):
    runner, _ = _runner(tmp_path, _values(finished=False))
    events = _run(runner, _request(resume=True))
    payload = json.loads(events[0]['payload_json'])
    assert len(payload['shots']) == 5 and payload['recovery_events'] == []


def test_committed_director_result_survives_lost_delivery(tmp_path):
    # Exercise actual LangGraph checkpoint ordering. Interrupt just after plan;
    # deliberately discard that stream's output to model a lost plan delivery.
    calls = []
    state = recovery_checkpoint()
    graph = StateGraph(PipelineState)
    def plan(value):
        calls.append('plan')
        return {'shots': state['shots'], 'events': state['events'][:1]}
    def finish(value):
        calls.append('finish')
        return {'finished': True, 'events': [make_event(value, node='advance', message='done')]}
    graph.add_node('plan', plan)
    graph.add_node('finish', finish)
    graph.add_edge(START, 'plan')
    graph.add_edge('plan', 'finish')
    graph.add_edge('finish', END)
    app = graph.compile(checkpointer=MemorySaver(), interrupt_after=['plan'])
    runner, _ = _runner(tmp_path, None)
    runner._app = app
    list(runner.run(_request()))  # consumer loses all these events
    assert calls == ['plan']
    events = list(runner.run(_request(resume=True)))
    assert events[0]['node'] == 'plan'
    assert len(json.loads(events[0]['payload_json'])['shots']) == 3
    assert calls == ['plan', 'finish']
