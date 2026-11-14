"""Typed model choices; unsupported facets never acquire source authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FactSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fact_id: str = Field(min_length=1, max_length=80)
    facet: Literal['snapshot_point', 'continuous_duration', 'incident_count', 'release_mechanism', 'current_state']
    assertion: Literal['selected', 'unknown']


def evaluate_selections(report, admitted_facts):
    choices=report.get('fact_selections') or []
    if not choices:
        return {'status':'not_provided','decisions':[],'admitted_point_ids':[]}
    ids={fact['fact_id'] for fact in admitted_facts}
    decisions=[];admitted=set()
    for raw in choices:
        # Persisted legacy/corrupted shapes cannot acquire authority either.
        try: choice=FactSelection.model_validate(raw)
        except ValueError:
            decisions.append({'status':'not_admitted','reason':'invalid_selection_shape'})
            continue
        item=choice.model_dump()
        if choice.assertion=='unknown':
            item.update(status='not_asserted',reason='model_declared_unknown')
        elif choice.facet!='snapshot_point':
            item.update(status='not_admitted',reason='unsupported_fact_facet')
        elif choice.fact_id not in ids:
            item.update(status='not_admitted',reason='point_not_admitted')
        else:
            item.update(status='admitted_point',reason='qualified_delivered_point')
            admitted.add(choice.fact_id)
        decisions.append(item)
    return {'status':'evaluated','decisions':decisions,'admitted_point_ids':sorted(admitted)}
