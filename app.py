from contextlib import asynccontextmanager
from pathlib import Path
import threading, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
import lab
import investigation
import incidents
from metrics import counter_window

stop=threading.Event();operation_lock=threading.Lock()
task_progress={}
def poll():
    while not stop.is_set():
        try:lab.collect()
        except Exception:pass
        stop.wait(20)
@asynccontextmanager
async def lifespan(app):
    stop.clear();t=threading.Thread(target=poll,daemon=True);t.start()
    yield
    stop.set()
app=FastAPI(title='DB Evidence Lab',lifespan=lifespan)
app.mount('/static', StaticFiles(directory=Path(__file__).parent/'web'), name='static')
@app.middleware('http')
async def local_only(request:Request,call_next):
    if request.headers.get('host','').split(':')[0] not in ['127.0.0.1','localhost','testserver']:
        return JSONResponse({'detail':'local host only'},status_code=403)
    if request.method=='POST':
        origin=request.headers.get('origin')
        if origin and origin not in ['http://127.0.0.1:8765','http://localhost:8765']:
            return JSONResponse({'detail':'cross-origin operation blocked'},status_code=403)
    return await call_next(request)
@app.get('/')
def home():return FileResponse(Path(__file__).parent/'web/index.html')
@app.get('/api/state')
def state():
    snapshots=[s for s in lab.records('snapshot',100) if not s['data'].get('purpose')][:40]
    window=counter_window(snapshots[1],snapshots[0]) if len(snapshots)>1 else {'status':'incomparable','reason':'need_two_samples'}
    return {'snapshots':snapshots,'rate_window':window,'analyses':lab.records('analysis',10),'investigations':lab.records('investigation',10),'drills':lab.records('drill',10),'incidents':lab.records('incident',10),'incident_tests':lab.records('incident_test',20),'recoveries':lab.records('recovery',10),'busy':operation_lock.locked(),'task_progress':task_progress}
@app.get('/api/task')
def task():return {'busy':operation_lock.locked(),'progress':task_progress}
@app.post('/api/incident/{scenario}')
def incident(scenario:str):
    if scenario not in ['lock','index']:raise HTTPException(404)
    def update(value):
        global task_progress
        task_progress=value
    return run(lambda:incidents.run_case(scenario,update))
@app.get('/api/record/{record_id}')
def record(record_id:str):
    try:return lab.get_record(record_id)
    except ValueError:raise HTTPException(404,'record not found')
def run(fn):
    if not operation_lock.acquire(False):raise HTTPException(409,'已有任务运行，请稍后重试')
    try:return fn()
    except Exception as e:
        lab.store('error',{'operation':fn.__name__ if hasattr(fn,'__name__') else 'task','error_type':type(e).__name__})
        raise HTTPException(500,'任务失败：'+type(e).__name__+'；检查本地实验环境与配置')
    finally:operation_lock.release()
@app.post('/api/collect')
def collect():return run(lab.collect)
class Analysis(BaseModel):
    snapshot_id:str
    question:str=Field(default='分析当前数据库风险，引用证据并明确未知。',max_length=2000)
@app.post('/api/analyze')
def analyze(body:Analysis):return run(lambda:lab.analyze(body.snapshot_id,body.question))
@app.post('/api/investigate')
def investigate(body:Analysis):return run(lambda:investigation.investigate(body.snapshot_id,body.question))
@app.post('/api/drill/{scenario}')
def drill(scenario:str):
    if scenario not in ['lock','index']:raise HTTPException(404)
    return run(lab.lock_drill if scenario=='lock' else lab.slow_drill)
@app.post('/api/recovery/{mode}')
def recovery(mode:str):
    if mode not in ['normal','corrupt']:raise HTTPException(404)
    return run(lambda:lab.recovery(mode=='corrupt'))
@app.post('/api/recovery/{record_id}/analyze')
def explain_recovery(record_id:str):return run(lambda:lab.analyze_recovery(record_id))
