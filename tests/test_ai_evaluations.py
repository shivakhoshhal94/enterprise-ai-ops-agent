"""Scripted-provider contract evaluations, not measurements of live model accuracy."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine

from enterprise_ai_ops_agent.ai.assistant import AssistantError, ask_business_assistant
from enterprise_ai_ops_agent.ai import tool_executor
from enterprise_ai_ops_agent.business.orders import OrderStatusCount
from enterprise_ai_ops_agent.business.customers import CustomerOrderHistory

SCENARIOS = json.loads((Path(__file__).resolve().parents[1]/'evals/tool_selection.json').read_text(encoding='utf-8-sig'))


@pytest.mark.parametrize('scenario',SCENARIOS,ids=[s['id'] for s in SCENARIOS])
def test_tool_selection_contract(scenario,monkeypatch,response_factory,fake_client):
    engine=MagicMock(spec=Engine)
    handlers={
        'get_order_status_summary':MagicMock(return_value=[OrderStatusCount(order_status='canceled',order_count=625)]),
        'get_customer_order_history':MagicMock(return_value=CustomerOrderHistory(customer_unique_id='X',found=False,customer_record_count=0,order_count=0,orders=[])),
        'get_order_financial_summary':MagicMock(return_value=None),
    }
    for name,handler in handlers.items():
        monkeypatch.setitem(tool_executor.HANDLERS,name,handler)
    call=scenario['scripted_call']
    responses=[response_factory(calls=[(call['name'],call['arguments'])])]
    if 'scripted_answer' in scenario:
        responses.append(response_factory(answer=scenario['scripted_answer']))
    client=fake_client(responses)
    if 'expected_failure' in scenario:
        with pytest.raises(AssistantError) as caught:
            ask_business_assistant(scenario['question'],client=client,engine=engine,model='fake-model')
        assert str(caught.value)==scenario['expected_failure']
        engine.connect.assert_not_called()
        assert not any(handler.called for handler in handlers.values())
        return
    result=ask_business_assistant(scenario['question'],client=client,engine=engine,model='fake-model')
    assert result.tool_calls[0].tool_name==scenario['expected_tool']
    assert result.tool_calls[0].arguments==scenario['expected_arguments']
    assert result.answer==scenario['scripted_answer']
    assert client.responses.create.call_args_list[0].kwargs['input'][0]['content']==scenario['question']
    assert handlers[scenario['expected_tool']].call_count==1


def assert_payment_grain_contract(instructions):
    """Test the general domain contract sent to the model, not one trap sentence."""
    rules = ' '.join(instructions.lower().split())
    assert re.search(r'one order_payments row.*one recorded payment entry', rules)
    assert re.search(r'payment_value is.*amount for.*payment entry', rules)
    assert re.search(r'order may have multiple payment entries', rules)
    assert re.search(r'order-level recorded payment total.*sum of payment_value.*all payment rows', rules)
    assert re.search(r'never multiply payment_value by payment_installments', rules)
    assert re.search(r'payment_installments is.*count.*payment entry, not a multiplier', rules)
    assert not re.search(r'payment_value (?:is|represents) (?:the )?total amount paid for (?:the )?(?:entire )?order', rules)


def test_payment_grain_contract_reaches_model_without_forcing_tool(response_factory, fake_client):
    engine = MagicMock(spec=Engine)
    client = fake_client([response_factory(answer=(
        'Installments do not multiply the recorded amount for a payment entry. '
        'Sum the order\'s payment entries to obtain its recorded payment total.'
    ))])
    result = ask_business_assistant(
        'Explain payment entry amounts, installments, and order totals.',
        client=client, engine=engine, model='fake-model',
    )
    assert_payment_grain_contract(client.responses.create.call_args.kwargs['instructions'])
    assert result.tool_calls == []
    engine.connect.assert_not_called()


@pytest.mark.parametrize('wrong_definition', [
    'payment_value represents the total amount paid for the order.',
    'payment_value is the total amount paid for the entire order.',
])
def test_payment_grain_contract_rejects_order_total_definition(wrong_definition):
    from enterprise_ai_ops_agent.ai.assistant import INSTRUCTIONS

    # Even otherwise correct instructions must fail if the misleading definition returns.
    with pytest.raises(AssertionError):
        assert_payment_grain_contract(INSTRUCTIONS + '\n' + wrong_definition)
