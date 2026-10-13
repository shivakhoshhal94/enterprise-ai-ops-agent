"""Deterministic source AND section relevance checks; no LLM grading."""

def summarize_evaluation(cases: list[dict], ranked_hits: list[list[dict]]) -> dict:
    positive=[]; diagnostics=[]
    for case,hits in zip(cases,ranked_hits,strict=True):
        rank=next((index+1 for index,hit in enumerate(hits)
                   if hit['document_id']==case.get('source') and case.get('section','').casefold() in hit['section'].casefold()),None)
        if case['kind']=='policy': positive.append(rank)
        diagnostics.append({'id':case['id'],'kind':case['kind'],'query':case['query'],'relevant_rank':rank,
                            'top_hits':hits[:5],'expected_behavior':case.get('expected_behavior')})
    count=len(positive)
    complete=all(len(hits)<50 for hits in ranked_hits)
    return {'policy_cases':count,'full_corpus_ranked':complete,'metrics':{**{f'Hit@{k}':sum(rank is not None and rank<=k for rank in positive)/count for k in (1,3,5)},
            'MRR' if complete else 'MRR@50':sum(1/rank if rank is not None else 0 for rank in positive)/count},'diagnostics':diagnostics,
            'metric_scope':'12 policy cases; relevance requires expected source AND section. Boundary/unrelated cases are separate uncalibrated diagnostics.'}
