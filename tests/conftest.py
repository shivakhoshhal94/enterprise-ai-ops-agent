"""SDK-shaped Responses fixtures; no network access or real credentials."""

from unittest.mock import MagicMock

import pytest
from openai.types.responses import Response


@pytest.fixture
def response_factory():
    def make(*, calls=None, answer=None, status='completed', extra_output=None):
        output = list(extra_output or [])
        for index, (name, arguments) in enumerate(calls or []):
            output.append({'type':'function_call', 'id':f'fc_{index}', 'call_id':f'call_{index}',
                           'name':name, 'arguments':arguments, 'status':'completed'})
        if answer is not None:
            output.append({'type':'message', 'id':'msg_final', 'role':'assistant', 'status':'completed',
                           'content':[{'type':'output_text','text':answer,'annotations':[],'logprobs':[]}]})
        return Response.model_validate({
            'id':'resp_fake','object':'response','created_at':0,'model':'fake-test-model',
            'status':status,'output':output,'parallel_tool_calls':False,'tool_choice':'auto','tools':[],
        })
    return make


@pytest.fixture
def fake_client():
    def make(responses):
        client = MagicMock()
        client.responses.create.side_effect = responses
        return client
    return make
