"""Independent desktop streaming and bounded, ordered, nonblocking input."""
import hashlib
import io
import queue
import threading
import time
import uuid
from collections import deque

class ScreenStream:
    def __init__(self, desktop, fps=12, quality=72):
        self.desktop, self.fps, self.quality = desktop, fps, quality
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.seq = 0
        self.jpeg = None
        self.error = None
        self.captured_at = 0
        self.capture_ms = 0
        self.frames = 0
        self.bytes_sent = 0
        self.started_at = time.monotonic()
        self.wanted = time.monotonic()  # last time someone asked for frames
        self.thread = threading.Thread(target=self._loop, name='desktop-stream', daemon=True)
        self.thread.start()

    def _loop(self):
        signature = None
        while not self.stopped.is_set():
            start = time.monotonic()
            if self.jpeg is not None and start - self.wanted > 3:
                # Nobody is watching this desktop: do not spend CPU capturing and encoding it.
                self.stopped.wait(0.25); continue
            try:
                image = self.desktop.capture()
                # Detect changes in the full frame; small text changes must not disappear.
                new_signature = hashlib.blake2s(image.tobytes(), digest_size=12).digest()
                if new_signature != signature or time.monotonic()-self.captured_at > 2:
                    buf=io.BytesIO(); image.save(buf,'JPEG',quality=self.quality)
                    with self.condition:
                        self.jpeg=buf.getvalue(); self.seq+=1; self.frames+=1
                        self.captured_at=time.monotonic(); self.error=None
                        self.capture_ms=round((time.monotonic()-start)*1000,2)
                        self.condition.notify_all()
                    signature=new_signature
            except Exception as exc:
                with self.condition:
                    self.error=str(exc); self.condition.notify_all()
            self.stopped.wait(max(0.01,1/self.fps-(time.monotonic()-start)))

    def next_frame(self, after=-1, timeout=2):
        self.wanted = time.monotonic()
        with self.condition:
            if after == -1 and self.jpeg is not None and self.wanted - self.captured_at > 1:
                after = self.seq  # the last frame is from before an idle pause: wait for a fresh one
            self.condition.wait_for(lambda:(self.jpeg is not None and self.seq!=after) or self.stopped.is_set(),timeout=timeout)
            return self.seq, self.jpeg

    def metrics(self):
        with self.condition:
            return {'target_fps':self.fps,'frames_encoded':self.frames,'capture_ms':self.capture_ms,
                    'frame_age_ms':round((time.monotonic()-self.captured_at)*1000) if self.jpeg else None,
                    'bytes_sent':self.bytes_sent,'error':self.error}

    def close(self):
        self.stopped.set()
        with self.condition:self.condition.notify_all()
        self.thread.join(timeout=2)

class InputQueue:
    def __init__(self, engine, size=64):
        self.engine=engine
        self.queue=queue.Queue(maxsize=size)
        self.lock=threading.RLock()
        self.jobs={}
        self.completed=0
        self.latencies=deque(maxlen=200)
        self.stopped=threading.Event()
        self.thread=threading.Thread(target=self._loop,name='desktop-input',daemon=True)
        self.thread.start()

    def submit(self, action):
        from app.core import validate_action
        action=validate_action(action)
        job={'id':uuid.uuid4().hex,'status':'queued','action':action,'accepted':time.monotonic(),'cancel':threading.Event()}
        with self.lock:
            if len(self.jobs)>200:
                self.jobs={k:v for k,v in self.jobs.items() if v['status'] in ('queued','running')}
            self.jobs[job['id']]=job
            try:self.queue.put_nowait(job)
            except queue.Full:
                del self.jobs[job['id']]
                raise ValueError('Input queue is full. Wait for pending actions or press Stop.')
        return {'id':job['id'],'status':'queued'}

    def _loop(self):
        while not self.stopped.is_set():
            try:job=self.queue.get(timeout=.1)
            except queue.Empty:continue
            try:
                if job['cancel'].is_set():raise InterruptedError()
                with self.lock:job['status']='running'
                start=time.monotonic()
                self.engine.act(job['action'],'you',job['cancel'])
                with self.lock:
                    job.update(status='completed',execute_ms=round((time.monotonic()-start)*1000,2))
                    self.latencies.append((time.monotonic()-job['accepted'])*1000)
                    self.completed+=1
            except InterruptedError:
                with self.lock:job['status']='stopped'
            except Exception as exc:
                with self.lock:job.update(status='failed',error=str(exc))
                self.engine.event('Input failed: '+str(exc),'error')
            finally:self.queue.task_done()

    def cancel(self):
        with self.lock:
            for job in self.jobs.values():
                if job['status'] in ('queued','running'):job['cancel'].set()

    def busy(self):
        with self.lock:return any(j['status'] in ('queued','running') for j in self.jobs.values())

    def metrics(self):
        with self.lock:
            vals=sorted(self.latencies)
            return {'pending':sum(j['status'] in ('queued','running') for j in self.jobs.values()),
                    'completed':self.completed,'completion_p50_ms':round(vals[len(vals)//2],2) if vals else None,
                    'completion_p95_ms':round(vals[min(len(vals)-1,int(len(vals)*.95))],2) if vals else None,
                    'recent':[{k:v for k,v in j.items() if k in ('id','status','execute_ms','error')}
                              for j in list(self.jobs.values())[-8:]]}

    def close(self):
        self.cancel();self.stopped.set();self.thread.join(timeout=2)
