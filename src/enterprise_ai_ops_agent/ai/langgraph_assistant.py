"""Experimental request-local LangGraph loop using the existing controlled tools.

No checkpointer, database access, action execution or additional tool definitions.
The stable Responses implementation remains untouched. Keep orchestration policy
parity covered by the shared fake-provider regression tests.
"""

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI, OpenAIError
from openai.types.responses import Response, ResponseFunctionToolCall
from sqlalchemy import Engine

from enterprise_ai_ops_agent.ai.assistant import (
    AssistantError, AssistantProviderError, AssistantToolError, AssistantKnowledgeError,
    AssistantSupportError, AssistantResult, MAX_TOOL_ROUNDS, INSTRUCTIONS,
    KNOWLEDGE_POLICY, SUPPORT_POLICY, PROPOSAL_ROUTING_POLICY,
)
from enterprise_ai_ops_agent.ai.tool_executor import (
    execute_tool, AnalyticalProposalError, KnowledgeToolError, SupportToolError,
    ToolExecutionError, ToolCallTrace,
)
from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS
from enterprise_ai_ops_agent.ai.schema_context import analytical_schema_context
from enterprise_ai_ops_agent.ai.presentation_data import PresentationData, project_tool_result
from enterprise_ai_ops_agent.ai.knowledge_contracts import (
    KnowledgeSource, KnowledgeEvidence, KnowledgeToolResult, financial_evidence_answer,
)
from enterprise_ai_ops_agent.ai.support_contracts import (
    ExternalSource, requested_order_ids, support_followup_scope, support_answer,
)
from enterprise_ai_ops_agent.integrations.support_tickets.contracts import SupportContext
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
from enterprise_ai_ops_agent.config import Settings


class AgentState(TypedDict):
    # Provider continuation items stay internal and are never returned to HTTP clients.
    inputs: list[Any]
    trace: list[ToolCallTrace]
    presentation: list[PresentationData]
    action_proposal: OrderReviewCaseProposal | None
    sources: dict[tuple[str, str], KnowledgeSource]
    knowledge_requested: bool
    evidence: list[KnowledgeEvidence]
    financial_data: dict | None
    support_contexts: dict[str, SupportContext]
    external: dict[str, ExternalSource]
    analytical_correction_pending: bool
    analytical_error_seen: bool
    round_number: int
    response: Response | None
    call: ResponseFunctionToolCall | None
    result: AssistantResult | None


def ask_langgraph_assistant(
    question: str, *, client: OpenAI, engine: Engine, model: str,
    max_tool_rounds: int = MAX_TOOL_ROUNDS, settings: Settings | None = None,
) -> AssistantResult:
    """Execute a bounded agent/tools graph; return the shared API response model."""
    if not question.strip() or not model.strip():
        raise AssistantError("Question and model must be nonblank.")
    if not 1 <= max_tool_rounds <= MAX_TOOL_ROUNDS:
        raise AssistantError("Tool round limit must be between one and three.")
    support_scope = support_followup_scope(question)
    order_scope = requested_order_ids(question)
    instructions = "\n\n".join((
        INSTRUCTIONS, analytical_schema_context(), KNOWLEDGE_POLICY,
        SUPPORT_POLICY, PROPOSAL_ROUTING_POLICY,
    ))
    initial: AgentState = {
        "inputs": [{"role": "user", "content": question}], "trace": [],
        "presentation": [], "action_proposal": None, "sources": {},
        "knowledge_requested": False, "evidence": [], "financial_data": None,
        "support_contexts": {}, "external": {}, "analytical_correction_pending": False,
        "analytical_error_seen": False, "round_number": 0, "response": None,
        "call": None, "result": None,
    }

    def finish(state: AgentState, answer: str, *, refused_external=False) -> AssistantResult:
        if state['action_proposal'] is not None:
            proposal = state['action_proposal']
            answer = (
                f"Proposed an internal order-review case for order {proposal.order_id} "
                f"with {proposal.priority} priority. No case has been created and no action "
                "has been executed. Explicit human approval is required in a later workflow; "
                "approval and execution are not available here."
            )
        elif state['support_contexts']:
            answer = support_answer(list(state['support_contexts'].values()), financial=state['financial_data'], evidence=state['evidence'], refused=refused_external)
        if state['knowledge_requested']:
            if not state['support_contexts'] and state['action_proposal'] is None and (state['financial_data'] is not None) and state['evidence']:
                answer = financial_evidence_answer(state['financial_data'], state['evidence'])
            if not state['sources'] and all((item.tool_name == 'search_company_knowledge' for item in state['trace'])):
                answer = 'The available demo company knowledge does not answer this question.'
            answer += '\n\nKnowledge evidence is application-owned demo documentation, not official Olist policy.'
            if state['sources']:
                answer += '\nRetrieved demo sources:\n' + '\n'.join((f'- {source.title} — {source.section}' for source in state['sources'].values()))
        return AssistantResult(
            answer=answer, tool_calls=state['trace'], action_proposal=state['action_proposal'],
            presentation_data=state['presentation'], knowledge_sources=list(state['sources'].values()),
            external_sources=list(state['external'].values()),
        )

    def agent(state: AgentState) -> dict:
        try:
            from enterprise_ai_ops_agent.observability import timed
            with timed('provider'):
                state['response'] = client.responses.create(
                    model=model, instructions=instructions,
                    tools=([tool for tool in TOOL_SCHEMAS if tool['name'] in support_scope]
                           if state['support_contexts'] else TOOL_SCHEMAS),
                    input=list(state['inputs']), parallel_tool_calls=False, store=False,
                    max_output_tokens=1200,
                )
        except OpenAIError:
            raise AssistantProviderError('OpenAI request failed; check API access and configuration.') from None
        if state['response'].status != 'completed':
            raise AssistantProviderError('OpenAI response did not complete.')
        calls = [item for item in state['response'].output if item.type == 'function_call']
        if not calls:
            if not state['response'].output_text.strip():
                raise AssistantError('OpenAI returned no final answer.')
            return {**state, 'result': finish(state, state['response'].output_text)}
        if state['round_number'] == max_tool_rounds:
            raise AssistantError('Tool execution round limit exceeded.')
        if len(calls) != 1:
            raise AssistantError('Expected at most one tool call per round.')
        state['call'] = calls[0]
        return state

    def controlled_tools(state: AgentState) -> dict:
        call = state["call"]
        response = state["response"]
        state["round_number"] += 1
        # External text cannot grant authority beyond the original user request.
        if state['support_contexts'] and call.name not in support_scope:
            return {**state, 'result': finish(state, '', refused_external=True)}
        if call.name == 'get_support_context' or (state['support_contexts'] and call.name in ('get_order_financial_summary', 'propose_order_review_case')):
            try:
                arguments = json.loads(call.arguments)
                identity = arguments.get('order_id') if isinstance(arguments, dict) else None
                if identity not in order_scope or (call.name == 'get_support_context' and identity in state['support_contexts']):
                    if state['support_contexts']:
                        return {**state, 'result': finish(state, '', refused_external=True)}
                    raise AssistantToolError('Support lookup requires an order ID explicitly supplied by the user.')
            except (ValueError, TypeError):
                raise AssistantToolError('Invalid tool arguments.') from None
        if call.name == 'propose_order_review_case' and state['action_proposal'] is not None:
            raise AssistantToolError('Only one order-review proposal is supported per request.')
        if state['analytical_correction_pending'] and call.name != 'run_analytical_query':
            raise AssistantToolError('Analytical correction must use the guarded analytical tool.')
        state['analytical_correction_pending'] = False
        try:
            if call.name == 'get_support_context':
                executed = execute_tool(engine, call.name, call.arguments)
                context = SupportContext.model_validate_json(executed.output_json)
                state['support_contexts'][context.order_id] = context
                for source in executed.external_sources:
                    state['external'][source.order_id] = source
            elif call.name == 'search_company_knowledge':
                executed = execute_tool(engine, call.name, call.arguments, settings=settings, client=client)
                state['knowledge_requested'] = True
                state['evidence'].extend(KnowledgeToolResult.model_validate_json(executed.output_json).evidence)
                for source in executed.knowledge_sources:
                    state['sources'].setdefault((source.document, source.section), source)
            elif call.name == 'run_analytical_query':
                executed = execute_tool(engine, call.name, call.arguments, settings=settings)
            else:
                executed = execute_tool(engine, call.name, call.arguments)
        except AnalyticalProposalError as error:
            if state['analytical_error_seen']:
                raise AssistantToolError('Analytical query failed after its single correction opportunity.') from None
            state['analytical_error_seen'] = True
            state['analytical_correction_pending'] = True
            state['inputs'].extend(response.output)
            state['inputs'].append({'type': 'function_call_output', 'call_id': call.call_id, 'output': json.dumps({'error': str(error), 'correction_remaining': 1})})
            return state
        except SupportToolError as error:
            raise AssistantSupportError(str(error), code=error.code) from None
        except KnowledgeToolError as error:
            raise AssistantKnowledgeError(str(error)) from None
        except ToolExecutionError as error:
            raise AssistantToolError(str(error)) from None
        if executed.action_proposal is not None:
            state['action_proposal'] = executed.action_proposal
        else:
            state['trace'].append(executed.trace)
        projected = project_tool_result(call.name, executed.output_json)
        if projected is not None:
            state['presentation'].append(projected)
        if call.name == 'get_order_financial_summary':
            state['financial_data'] = json.loads(executed.output_json)
        state['inputs'].extend(response.output)
        state['inputs'].append({'type': 'function_call_output', 'call_id': call.call_id, 'output': executed.output_json})
        return state

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("controlled_tools", controlled_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", lambda state: END if state["result"] is not None else "controlled_tools")
    graph.add_conditional_edges("controlled_tools", lambda state: END if state["result"] is not None else "agent")
    # Separate from durable business approval: no checkpointing or resume surface.
    final = graph.compile().invoke(initial, config={"recursion_limit": 2 * max_tool_rounds + 4})
    return final["result"]
