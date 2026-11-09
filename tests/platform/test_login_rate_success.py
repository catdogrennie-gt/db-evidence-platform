from types import SimpleNamespace
from fastapi import HTTPException
import pytest


def test_successful_login_removes_only_its_own_rate_reservation():
    from platform_app.routes import auth
    req=SimpleNamespace(client=SimpleNamespace(host='test-public-demo'))
    auth._attempts.clear()
    for _ in range(7):
        reservation=auth._rate_limit(req,'public-demo')
        auth._successful_attempt(reservation)
    failures=[auth._rate_limit(req,'public-demo') for _ in range(4)]
    success=auth._rate_limit(req,'public-demo')
    auth._successful_attempt(success)
    assert list(auth._attempts[failures[0][0]])==[r[1] for r in failures]
    auth._rate_limit(req,'public-demo')
    with pytest.raises(HTTPException) as error:auth._rate_limit(req,'public-demo')
    assert error.value.status_code==429
    auth._attempts.clear()
