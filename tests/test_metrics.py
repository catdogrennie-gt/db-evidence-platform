import copy
import pytest
from metrics import counter_window


def sample(ts,uptime,questions,uuid='lab'):
    return {'id':str(ts),'data':{'evidence':[
        {'source':'mysql.identity','value':[{'uuid':uuid}]},
        {'source':'mysql.status','collected_at':ts,'value':[{'Variable_name':'Uptime','Value':str(uptime)},{'Variable_name':'Questions','Value':str(questions)}]}
    ]}}


def test_real_window_rate():
    r=counter_window(sample('2026-11-08T00:00:00+00:00',100,500),sample('2026-11-08T00:00:20+00:00',120,560))
    assert r['seconds']==20
    assert r['rates']['Questions']=={'delta':60,'per_second':3}


@pytest.mark.parametrize('new,reason',[
    (sample('2026-11-08T00:00:20+00:00',5,560),'instance_restarted'),
    (sample('2026-11-08T00:00:20+00:00',120,10),'counter_reset'),
    (sample('2026-11-08T00:00:20+00:00',120,560,'other'),'instance_changed'),
    (sample('2026-11-08T00:00:00+00:00',120,560),'nonpositive_interval'),
    ({},'missing_or_invalid_samples')])
def test_invalid_window_not_displayed_as_zero(new,reason):
    r=counter_window(sample('2026-11-08T00:00:00+00:00',100,500),new)
    assert r=={'status':'incomparable','reason':reason}
