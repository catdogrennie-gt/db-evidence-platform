"""Model settings accept the same bounded output budget as the runner."""
import pytest

from tests.platform.test_admin_scope import admin_graph, as_actor


@pytest.mark.parametrize('limit,status', [(4096, 201), (4097, 422)])
def test_model_output_limit_boundary_is_persisted_only_when_valid(admin_graph, limit, status):
    g = admin_graph
    g.config.model = {'provider': 'trusted', 'model': 'reviewed-model'}
    as_actor(g, 'global')
    before = g.client.get('/api/v1/model-profiles').json()['items'][0]
    assert before['max_tokens'] == 3000
    result = g.client.post('/api/v1/model-profiles', json={'max_tokens': limit})
    assert result.status_code == status, result.json()
    after = g.client.get('/api/v1/model-profiles').json()['items'][0]
    assert after['max_tokens'] == (limit if status == 201 else before['max_tokens'])
    if status == 422:
        assert result.json()['detail']['code'] == 'invalid_model_limit'


def test_saving_other_model_options_does_not_change_output_default(admin_graph):
    g = admin_graph
    g.config.model = {'provider': 'trusted', 'model': 'reviewed-model'}
    as_actor(g, 'global')
    result = g.client.post('/api/v1/model-profiles', json={'name': 'A configured profile'})
    assert result.status_code == 201 and result.json()['max_tokens'] == 3000
