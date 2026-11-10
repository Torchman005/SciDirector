from __future__ import annotations

import base64
import json
from concurrent import futures

import grpc
import httpx

from scidirector_ai.config import Settings
from scidirector_ai.grpc_server import AiDirectorServicer
from scidirector_ai.illustrations import ImageGenerator
from scidirector_ai.service import PipelineService
from scidirector.v1 import ai_service_pb2 as pb
from scidirector.v1 import ai_service_pb2_grpc as pbg
from test_illustrations import png, settings as image_settings
from test_fy_main_workflow import style


def test_real_grpc_recommendation_catalog_and_image_staging(style, tmp_path):
    service = PipelineService(Settings(_env_file=None, env='test', llm_provider='mock', postgres_dsn=''))
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    service.images = ImageGenerator(image_settings(), transport=httpx.MockTransport(handle))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    pbg.add_AiDirectorServiceServicer_to_server(AiDirectorServicer(service), server)
    port = server.add_insecure_port('127.0.0.1:0')
    server.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            stub = pbg.AiDirectorServiceStub(channel)
            catalog = json.loads(stub.GetImageProviders(pb.ImageProvidersRequest(), timeout=10).providers_json)
            assert len(catalog['providers']) == 2
            match = stub.RecommendFyStyle(pb.RecommendFyStyleRequest(
                raw_script='通过尺寸线和技术原理，逐步解释这台机械装置怎样完成工作。'), timeout=20)
            assert json.loads(match.recommendation_json)['style_id'] == 'X17'
            response = stub.GenerateIllustration(pb.GenerateIllustrationRequest(provider_id='relay',
                prompt='一台蒸汽机', size='1024x1024', fy_style_id='X17', output_dir=str(tmp_path)), timeout=20)
            assert response.fy_style_sha256 == style.fy_style_source['sha256']
            assert response.fy_style_id == 'X17' and (tmp_path / 'illustration.png').exists()
            assert len(calls) == 1 and '完整画风原文。' in calls[0]['prompt']
    finally:
        server.stop(0).wait()
        service.close()
