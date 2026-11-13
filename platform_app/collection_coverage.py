"""Current selected-item metadata; neither historical coverage nor health proof."""
from datetime import datetime, timezone
from .common import iso


def selected_coverage(objects, mappings, *, now):
    gaps=[]
    for instance in objects:
        mapping=mappings.get(instance.id)
        if mapping is None:continue
        for item in mapping.items:
            reason=None;stamp=None
            try:
                clock=int(item.get('lastclock',0))
                if clock>0:stamp=datetime.fromtimestamp(clock,timezone.utc).replace(tzinfo=None)
            except (ValueError,TypeError,OverflowError,OSError):pass
            if item.get('missing'):reason='item_missing'
            elif str(item.get('status','0'))!='0':reason='item_disabled'
            elif str(item.get('state','0'))!='0' or item.get('available')is False:reason='item_unsupported'
            elif stamp is None:reason='no_samples'
            elif item.get('interval_verified')is not True:reason='sampling_interval_unverified'
            else:
                try:interval=max(1,int(item.get('interval',60)))
                except (TypeError,ValueError,OverflowError):interval=60
                if stamp>now:reason='sample_time_in_future'
                elif (now-stamp).total_seconds()>max(60,interval*3):reason='sample_stale'
            if reason:
                gaps.append({'instance_id':instance.id,'object_name':instance.name,
                    'mapping_id':mapping.id,'host_name':str(item.get('host_name',''))[:255],
                    'itemid':str(item.get('itemid','')),'item_name':str(item.get('name',''))[:255],
                    'reason':reason,'last_sample_at':iso(stamp)})
    return {'status':'partial'if gaps else'unknown','missing_count':len(gaps),
        'items':gaps[:200],'truncated':len(gaps)>200,'checked_at':iso(now),
        'basis':'current_selected_item_metadata'}
