"""Scripted-provider contract evaluations, not live-model routing measurements."""

import json
from pathlib import Path

import pytest

from enterprise_ai_ops_agent.ai import assistant
from test_action_proposals import snapshot
from test_operations import database, ORDER

SCENARIOS=json.loads((Path(__file__).resolve().parents[1]/'evals/action_proposals.json').read_text())


@pytest.mark.parametrize('scenario',SCENARIOS,ids=[case['id'] for case in SCENARIOS])
def test_action_proposal_contract(scenario,database,response_factory,fake_client):
    before=snapshot(database)
    question=scenario['question'].format(order_id=ORDER)
    responses=[]
    if scenario['tool'] is not None:
        arguments={'order_id':ORDER,**scenario['arguments']}
        responses.append(response_factory(calls=[(scenario['tool'],json.dumps(arguments))]))
    if 'scripted_answer' in scenario:
        responses.append(response_factory(answer=scenario['scripted_answer']))
    provider=fake_client(responses)
    if 'expected_error' in scenario:
        with pytest.raises(assistant.AssistantToolError,match=scenario['expected_error']):
            assistant.ask_business_assistant(question,client=provider,engine=database,model='fake')
    else:
        result=assistant.ask_business_assistant(question,client=provider,engine=database,model='fake')
        if scenario['tool'] is None:
            assert result.action_proposal is None
            assert result.answer==scenario['scripted_answer']
        else:
            assert result.action_proposal.order_id==ORDER
            assert result.action_proposal.reason==scenario['arguments']['reason']
            assert result.action_proposal.priority==scenario['expected_priority']
            assert result.action_proposal.requires_approval is True
            assert 'No case has been created' in result.answer
            assert 'approval bypassed' not in result.answer
        assert result.tool_calls==[]
    assert provider.responses.create.call_args_list[0].kwargs['input'][0]['content']==question
    assert snapshot(database)==before


def test_intent_semantics_reach_provider(response_factory,fake_client,database):
    provider=fake_client([response_factory(answer='Please clarify your concern.')])
    assistant.ask_business_assistant('Investigate',client=provider,engine=database,model='fake')
    rules=provider.responses.create.call_args.kwargs['instructions']
    assert 'qualify allegations as concerns requiring review' in rules
    assert 'not confirmed fraud' in rules
    assert 'Do not substitute an' in rules and 'order-review proposal for an unsupported action' in rules
    assert 'Approval/execution are unavailable here' in rules
    assert 'Do not delay a clear review proposal solely because reason or priority was omitted' in rules
    assert 'neutral medium priority when unspecified' in rules
    assert 'explicitly say the requested external action is unavailable' in rules
    assert 'Do not offer a review case' in rules
    assert 'Required tool fields do not mean the user' in rules
    assert '"medium"' in rules
    assert rules.endswith(assistant.PROPOSAL_ROUTING_POLICY)
