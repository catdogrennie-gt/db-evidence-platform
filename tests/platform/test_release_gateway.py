"""Release boundary tests use real HTTP servers and, when supplied, real Caddy.

Set RELEASE_GATEWAY_CADDY to the verified official executable for integration checks.
No test touches the application's configured database, public ports or tunnels.
"""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, Request, build_opener

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / "scripts/platform/release_gateway.py"
    assert path.is_file(), "The checked release gateway has not been implemented"
    spec = importlib.util.spec_from_file_location("release_gateway", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def request(url, value=None):
    data = None if value is None else json.dumps(value).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(req, timeout=3) as response:
        return response.status, response.read()


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def upstreams():
    running = []

    def create(revision):
        state = {"ready": True, "revision": revision}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ready":
                    body = {"ready": state["ready"]}
                    status = 200 if state["ready"] else 503
                else:
                    body, status = {"revision": state["revision"]}, 200
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}", state

    yield create
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def release(tmp_path, name, gateway):
    source = tmp_path / (name + "-build")
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(
        f'<html><script type="module" src="/assets/{name}.js"></script></html>',
        encoding="utf-8",
    )
    (source / "assets" / (name + ".js")).write_text(name + "-javascript", encoding="utf-8")
    return gateway.snapshot_frontend(source, tmp_path / "releases", name)


def spec(upstream, *, listen=None, admin=None, frontend=None, retained=None, revision="blue"):
    return {
        "name": "shadow",
        "listen": listen or f"127.0.0.1:{port()}",
        "admin": admin or f"127.0.0.1:{port()}",
        "target": {"origin": upstream, "ready_path": "/ready", "ready_equals": {"ready": True}},
        "gateway_probe": {"path": "/api/status", "equals": {"revision": revision}},
        "frontend": str(frontend) if frontend else None,
        "retained_frontends": [str(p) for p in (retained or [])],
    }


def test_generation_refuses_public_admin_or_upstream():
    gateway = module()
    for key, bad in [("admin", "0.0.0.0:19199"), ("listen", ":19198")]:
        value = spec("http://127.0.0.1:19197")
        value[key] = bad
        with pytest.raises(gateway.ReleaseError):
            gateway.build_config(value)
    value = spec("http://example.com:80")
    with pytest.raises(gateway.ReleaseError):
        gateway.build_config(value)


def test_fixed_front_door_requires_loopback_management():
    gateway = module()
    assert hasattr(gateway, "build_front_door"), "A non-reloaded public listener is required"
    with pytest.raises(gateway.ReleaseError):
        gateway.build_front_door("127.0.0.1:19001", "http://127.0.0.1:19002", "0.0.0.0:19003")


def test_public_read_only_gateway_blocks_non_read_methods_before_proxy():
    gateway = module()
    value = spec("http://127.0.0.1:19197")
    value["public_read_only"] = True
    routes = gateway.build_config(value)["apps"]["http"]["servers"]["edge"]["routes"]
    guard = routes[0]
    assert guard["match"] == [{"not": [{"method": ["GET", "HEAD", "OPTIONS"]}]}]
    assert guard["handle"] == [{"handler": "static_response", "status_code": 403}]
    assert guard["terminal"] is True
    assert routes[-1]["handle"][0]["handler"] == "reverse_proxy"


def test_normal_platform_gateway_keeps_login_methods_available():
    gateway = module()
    value = spec("http://127.0.0.1:19197")
    routes = gateway.build_config(value)["apps"]["http"]["servers"]["edge"]["routes"]
    assert not any(route.get("match") == [{"not": [{"method": ["GET", "HEAD", "OPTIONS"]}]}]
                   for route in routes)


def test_public_read_only_option_requires_boolean():
    gateway = module()
    value = spec("http://127.0.0.1:19197")
    value["public_read_only"] = "false"
    with pytest.raises(gateway.ReleaseError):
        gateway.build_config(value)


@pytest.mark.parametrize('frontend', [False, True])
@pytest.mark.parametrize('read_only', [False, True])
def test_real_caddy_public_read_only_stops_writes_before_any_upstream(tmp_path, frontend, read_only):
    """Use isolated ephemeral listeners; never send a request to the real Lab."""
    from urllib.error import HTTPError
    binary=os.environ.get('RELEASE_GATEWAY_CADDY')
    if not binary or not Path(binary).is_file():
        pytest.skip('Set RELEASE_GATEWAY_CADDY to run verified Caddy integration')
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def reply(self):
            calls.append((self.command,self.path))
            raw=b'{"accepted":true}'
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
            if self.command!='HEAD':self.wfile.write(raw)
        do_GET=do_HEAD=do_OPTIONS=do_POST=do_PUT=do_PATCH=do_DELETE=do_TRACE=do_BREW=reply
        def log_message(self,*args):pass
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
    gateway=module();value=spec(f'http://127.0.0.1:{upstream.server_port}',
        frontend=release(tmp_path,'read-only-build',gateway) if frontend else None)
    if read_only:value['public_read_only']=True
    path=tmp_path/'caddy-test.json';path.write_text(json.dumps(gateway.build_config(value)))
    process=None
    try:
        process=subprocess.Popen([binary,'run','--config',str(path)],stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
            env=dict(os.environ,XDG_DATA_HOME=str(tmp_path/'caddy-data')))
        for _ in range(60):
            if process.poll() is not None:pytest.fail('Isolated Caddy exited before readiness')
            try:
                request('http://'+value['listen']+'/api/status');break
            except OSError:time.sleep(.05)
        else:pytest.fail('Isolated Caddy did not become ready')
        calls.clear();client=build_opener(ProxyHandler({}));base='http://'+value['listen']
        for method in ('GET','HEAD','OPTIONS'):
            with client.open(Request(base+'/api/read',method=method),timeout=3) as response:
                assert response.status==200
        assert [method for method,_ in calls]==['GET','HEAD','OPTIONS']
        if read_only:
            for method in ('POST','PUT','PATCH','DELETE','TRACE','BREW'):
                for target in ('/api/switch','/api/v1/login','/outside','/%61pi/switch'):
                    with pytest.raises(HTTPError) as error:
                        client.open(Request(base+target,method=method),timeout=3)
                    assert error.value.code==403
            assert len(calls)==3,'A blocked write must never reach any upstream'
        else:
            with client.open(Request(base+'/api/v1/login',data=b'{}',method='POST'),timeout=3) as response:
                assert response.status==200
            assert calls[-1]==('POST','/api/v1/login'),'Default B login must remain available'
    finally:
        if process is not None:
            process.terminate();process.wait(timeout=10)
        upstream.shutdown();upstream.server_close();thread.join(timeout=3)


def test_snapshot_pins_build_and_detects_later_edit(tmp_path):
    gateway = module()
    snapshot = release(tmp_path, "blue", gateway)
    gateway.verify_frontend(snapshot)
    (snapshot / "frontend/assets/blue.js").write_text("changed", encoding="utf-8")
    with pytest.raises(gateway.ReleaseError):
        gateway.verify_frontend(snapshot)
    with pytest.raises(gateway.ReleaseError):
        gateway.snapshot_frontend(tmp_path / "blue-build", tmp_path / "releases", "blue")


def test_snapshot_rejects_missing_index_asset_and_path_escape(tmp_path):
    gateway = module()
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text('<script src="/assets/missing.js"></script>')
    with pytest.raises(gateway.ReleaseError):
        gateway.snapshot_frontend(source, tmp_path / "releases", "one")
    with pytest.raises(gateway.ReleaseError):
        gateway.snapshot_frontend(source, tmp_path / "releases", "../outside")


def test_snapshot_refuses_private_material_in_build_directory(tmp_path):
    gateway = module()
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("<html>public build</html>")
    (source / ".env").write_text("NOT_A_REAL_SECRET=private")
    with pytest.raises(gateway.ReleaseError):
        gateway.snapshot_frontend(source, tmp_path / "releases", "one")


def test_preflight_checks_real_ready_value(upstreams):
    gateway = module()
    origin, state = upstreams("blue")
    value = spec(origin)
    gateway.preflight(value)
    state["ready"] = False
    with pytest.raises(gateway.ReleaseError):
        gateway.preflight(value)


@pytest.fixture
def caddy_gateway(tmp_path, upstreams):
    gateway = module()
    binary = os.environ.get("RELEASE_GATEWAY_CADDY")
    if not binary or not Path(binary).is_file():
        pytest.skip("Set RELEASE_GATEWAY_CADDY to run verified Caddy integration")
    blue, blue_state = upstreams("blue")
    green, green_state = upstreams("green")
    blue_build = release(tmp_path, "blue", gateway)
    green_build = release(tmp_path, "green", gateway)
    blue_spec = spec(blue, frontend=blue_build)
    green_spec = spec(
        green, listen=blue_spec["listen"], admin=blue_spec["admin"],
        frontend=green_build, retained=[blue_build], revision="green",
    )
    config = tmp_path / "caddy.json"
    config.write_text(json.dumps(gateway.build_config(blue_spec)), encoding="utf-8")
    log = (tmp_path / "caddy.log").open("wb")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [binary, "run", "--config", str(config)], stdout=log, stderr=log,
        creationflags=flags, env=dict(os.environ, XDG_DATA_HOME=str(tmp_path / "data")),
    )
    inner_url = "http://" + blue_spec["listen"]
    url = "http://127.0.0.1:" + str(port())
    front_config = tmp_path / "front-door.json"
    front_config.write_text(json.dumps(gateway.build_front_door(url[7:], inner_url, "127.0.0.1:" + str(port()))), encoding="utf-8")
    front = subprocess.Popen(
        [binary, "run", "--config", str(front_config)], stdout=log, stderr=log,
        creationflags=flags, env=dict(os.environ, XDG_DATA_HOME=str(tmp_path / "data")),
    )
    try:
        for _ in range(60):
            if process.poll() is not None:
                pytest.fail("Caddy exited: " + (tmp_path / "caddy.log").read_text(errors="replace"))
            try:
                if request(url + "/api/status")[0] == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Caddy did not become ready")
        manager = gateway.DeploymentManager(tmp_path / "runtime", "http://" + blue_spec["admin"], binary)
        manager.adopt(blue_spec)
        yield gateway, manager, blue_spec, green_spec, blue_state, green_state, url
    finally:
        front.terminate()
        front.wait(timeout=10)
        process.terminate()
        process.wait(timeout=10)
        log.close()


def test_unready_target_keeps_active_route(caddy_gateway):
    gateway, manager, blue, green, _, green_state, url = caddy_gateway
    before = manager.status()
    green_state["ready"] = False
    with pytest.raises(gateway.ReleaseError):
        manager.switch(green)
    assert manager.status() == before
    assert json.loads(request(url + "/api/status")[1]) == {"revision": "blue"}


def test_frontend_keeps_response_security_headers(caddy_gateway):
    *_, url = caddy_gateway
    with build_opener(ProxyHandler({})).open(url + "/", timeout=3) as response:
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in response.headers.get("Content-Security-Policy", "")


def test_fixed_front_door_retries_get_but_never_replays_post(tmp_path):
    from urllib.error import HTTPError

    gateway = module()
    binary = os.environ.get("RELEASE_GATEWAY_CADDY")
    if not binary or not Path(binary).is_file():
        pytest.skip("Set RELEASE_GATEWAY_CADDY to run verified Caddy integration")
    calls = {"GET": 0, "POST": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls["GET"] += 1
            if calls["GET"] == 1:
                self.close_connection = True
                return
            raw = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            calls["POST"] += 1
            self.close_connection = True

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    listen, admin = "127.0.0.1:" + str(port()), "127.0.0.1:" + str(port())
    config = tmp_path / "front.json"
    config.write_text(json.dumps(gateway.build_front_door(listen, f"http://127.0.0.1:{upstream.server_port}", admin)), encoding="utf-8")
    process = subprocess.Popen([binary, "run", "--config", str(config)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        for _ in range(60):
            try:
                request("http://" + admin + "/config/")
                break
            except OSError:
                time.sleep(0.05)
        assert request("http://" + listen + "/read")[1] == b'{"ok":true}'
        assert calls["GET"] == 2
        with pytest.raises(HTTPError) as error:
            request("http://" + listen + "/write", {"value": 1})
        assert error.value.code == 502
        assert calls["POST"] == 1
    finally:
        process.terminate()
        process.wait(timeout=10)
        upstream.shutdown()
        upstream.server_close()
        worker.join(timeout=3)


def test_switch_and_rollback_serve_continuously_and_keep_old_assets(caddy_gateway):
    _, manager, _, green, _, _, url = caddy_gateway
    observed, errors = [], []
    stop = threading.Event()

    def traffic():
        while not stop.is_set():
            try:
                status, body = request(url + "/api/status")
                observed.append((status, json.loads(body)["revision"]))
            except Exception as error:
                errors.append({"at": time.time(), "error": repr(error)})
            time.sleep(0.005)

    thread = threading.Thread(target=traffic)
    thread.start()
    try:
        time.sleep(0.08)
        manager.switch(green)
        assert json.loads(request(url + "/api/status")[1]) == {"revision": "green"}
        assert request(url + "/assets/blue.js")[1] == b"blue-javascript"
        assert request(url + "/assets/green.js")[1] == b"green-javascript"
        assert b"green.js" in request(url + "/dashboard/nested")[1]
        manager.rollback()
        assert json.loads(request(url + "/api/status")[1]) == {"revision": "blue"}
        assert request(url + "/assets/green.js")[1] == b"green-javascript"
        time.sleep(0.08)
    finally:
        stop.set()
        thread.join(timeout=4)
    assert not errors
    assert len(observed) >= 10
    assert all(status == 200 and revision in {"blue", "green"} for status, revision in observed)
    assert {revision for _, revision in observed} == {"blue", "green"}


def test_post_apply_probe_failure_restores_last_good_configuration(caddy_gateway):
    gateway, manager, _, green, _, _, url = caddy_gateway
    before = manager.status()
    green["gateway_probe"]["equals"] = {"revision": "not-the-requested-release"}
    with pytest.raises(gateway.ReleaseError):
        manager.switch(green)
    assert manager.status() == before
    assert json.loads(request(url + "/api/status")[1]) == {"revision": "blue"}


def test_out_of_band_config_change_is_not_overwritten(caddy_gateway):
    gateway, manager, blue, green, _, _, url = caddy_gateway
    changed = gateway.build_config(blue)
    changed["apps"]["http"]["servers"]["edge"]["idle_timeout"] = 120000000000
    request("http://" + blue["admin"] + "/load", changed)
    with pytest.raises(gateway.ReleaseError):
        manager.switch(green)
    current = json.loads(request("http://" + blue["admin"] + "/config/")[1])
    assert current["apps"]["http"]["servers"]["edge"]["idle_timeout"] == 120000000000
    assert json.loads(request(url + "/api/status")[1]) == {"revision": "blue"}


def test_interrupted_switch_can_explicitly_recover_known_previous_release(caddy_gateway):
    gateway, manager, blue, green, _, _, url = caddy_gateway
    assert hasattr(manager, "recover"), "Interrupted updates need checked recovery"
    state = json.loads((manager.runtime / "state.private.json").read_text())
    (manager.runtime / "pending.private.json").write_text(json.dumps({"before": state, "intended_spec": green}))
    request("http://" + blue["admin"] + "/load", gateway.build_config(green))
    with pytest.raises(gateway.ReleaseError, match="unfinished_switch"):
        manager.switch(green)
    manager.recover()
    assert json.loads(request(url + "/api/status")[1]) == {"revision": "blue"}
    assert request(url + "/assets/green.js")[1] == b"green-javascript"
    assert not (manager.runtime / "pending.private.json").exists()
