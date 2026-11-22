"""Non-blocking panel-only lifecycle operations for the owned DSH Web."""
from datetime import datetime,timezone
from pathlib import Path
import re
import threading
import time
import uuid

from . import web_launch

LABELS={"checking":"检查端口与实例","starting_worker":"启动监督进程", "waiting_address":"等待 DSH Web 返回启动地址",
        "authenticating":"验证 Web 连接认证", "ready":"DSH Web 已就绪", "stopping":"正在停止托管 DSH Web",
        "waiting_stop":"等待 Web 与监督进程退出", "stopped":"DSH Web 已停止", "failed":"操作失败"}

class OperationBusy(RuntimeError): pass

class WebControl:
    def __init__(self,state):
        self.state=Path(state)
        self.lock=threading.Lock()
        self.job=None

    def snapshot(self):
        with self.lock:
            if not self.job:
                return {"state":"idle","phase":"idle","message":"暂无进行中的操作","elapsed_seconds":0}
            job=self.job
            end=job.get("finished_clock",time.monotonic())
            return {key:(list(job[key]) if key=="stages" else job[key]) for key in ("id","operation","state","phase","message","started_at","stages") if key in job} | {"elapsed_seconds":round(max(0,end-job["started_clock"]),1)}

    def _update(self,identity,phase,state="running",error=None):
        with self.lock:
            if not self.job or self.job["id"]!=identity:
                return
            job=self.job
            job.update(state=state,phase=phase,message=LABELS.get(phase,phase))
            if not job["stages"] or job["stages"][-1]!=phase:
                job["stages"].append(phase)
            if error:
                message=re.sub(r"(?i)(token=)[^&\s]+",r"\1[REDACTED]",str(error))
                job["message"]=re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]+",r"\1[REDACTED]",message)[:500]
            if state!="running":
                job["finished_clock"]=time.monotonic()

    def _begin(self,operation,port=3080):
        with self.lock:
            if self.job and self.job["state"]=="running":
                if self.job["operation"]==operation:
                    return False,self.job["id"]
                raise OperationBusy("已有启动/停止操作进行中，请等待结束")
            identity=uuid.uuid4().hex
            self.job={"id":identity,"operation":operation,"state":"running","phase":"checking" if operation=="start" else "stopping",
                      "message":LABELS["checking" if operation=="start" else "stopping"],"started_at":datetime.now(timezone.utc).isoformat(),
                      "started_clock":time.monotonic(),"stages":[]}
        target=self._start if operation=="start" else self._stop
        threading.Thread(target=target,args=(identity,port),daemon=True,name="web-"+operation).start()
        return True,identity

    def start(self,port=3080):
        if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535:
            raise ValueError("Web 端口必须在 1–65535 之间")
        self._begin("start",port)
        return self.snapshot()

    def stop(self,confirm=False):
        if confirm is not True:
            raise ValueError("停止 Web 可能中断任务，需要明确 confirm=true")
        self._begin("stop")
        return self.snapshot()

    def _start(self,identity,port):
        try:
            self._update(identity,"checking")
            result=web_launch.launch(self.state,port,on_progress=lambda phase:self._update(identity,phase))
            self._update(identity,"authenticating")
            deadline=time.monotonic()+15
            delay=.1
            while True:
                if web_launch._port_in_use(port) and web_launch.probe_token(result.get("url") or "",timeout=2).get("ok"):
                    self._update(identity,"ready",state="ready")
                    return
                if time.monotonic()>deadline:
                    raise RuntimeError("Web 启动地址已获取，但连接认证未就绪；请查看日志或重新连接，实例不会被自动终止")
                time.sleep(delay)
                delay=min(delay*2,1)
        except Exception as error:
            self._update(identity,"failed",state="failed",error=error)

    def _stop(self,identity,port):
        try:
            self._update(identity,"stopping")
            status=web_launch.status(self.state,probe=False)
            if status.get("running") and not status.get("managed"):
                raise RuntimeError("此 Web 实例并非本面板托管，不提供终止操作")
            if not status.get("running"):
                self._update(identity,"stopped",state="stopped")
                return
            from ..core.secrets import read_secret_json
            record=read_secret_json(self.state/"web_launch.json")
            response=web_launch.stop_managed(self.state)
            if not response.get("stopping"):
                raise RuntimeError("托管监督进程未接受停止请求")
            self._update(identity,"waiting_stop")
            deadline=time.monotonic()+20
            delay=.05
            while web_launch._port_in_use(int(status["port"])) or web_launch._port_in_use(int(record["control_port"])):
                if time.monotonic()>deadline:
                    raise TimeoutError("停止未在 20 秒内确认；未执行额外或批量终止")
                time.sleep(delay)
                delay=min(delay*2,1)
            self._update(identity,"stopped",state="stopped")
        except Exception as error:
            self._update(identity,"failed",state="failed",error=error)
