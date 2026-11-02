"""16-way priority-aware non-blocking AI executor with Future compatibility.

High priority is numerical 0 (fresh live orders), manual=1, background=2.
One running request is never forcibly preempted; queued work can be cancelled
at shutdown without retrying potentially billed requests.
"""
from concurrent.futures import Future
from itertools import count
from queue import PriorityQueue,Empty
from threading import Lock, Thread

class PriorityExecutor:
    def __init__(self,max_workers=16,thread_name_prefix='bet-ai'):
        self.max_workers=max(1,min(32,int(max_workers)))
        self.queue=PriorityQueue()
        self._lock=Lock()
        self._sequence=count()
        self._closed=False
        self._workers=[]
        for i in range(self.max_workers):
            t=Thread(target=self._worker,name=f'{thread_name_prefix}_{i}',daemon=True)
            t.start();self._workers.append(t)

    def submit(self,fn,*args,priority=2,**kwargs):
        with self._lock:
            if self._closed:raise RuntimeError('AI executor already shut down')
            f=Future()
            self.queue.put((max(0,int(priority)),next(self._sequence),f,fn,args,kwargs))
            return f

    def _worker(self):
        while True:
            _,_,f,fn,args,kwargs=self.queue.get()
            try:
                if f is None:return
                if f.set_running_or_notify_cancel():
                    try:result=fn(*args,**kwargs)
                    except BaseException as exc:f.set_exception(exc)
                    else:f.set_result(result)
            finally:self.queue.task_done()

    def shutdown(self,wait=True,cancel_futures=False):
        with self._lock:
            if self._closed:return
            self._closed=True
            if cancel_futures:
                while True:
                    try:_,_,f,_,_,_=self.queue.get_nowait()
                    except Empty:break
                    if f is not None:f.cancel()
                    self.queue.task_done()
            for _ in self._workers:
                self.queue.put((10**9,next(self._sequence),None,None,(),{}))
        if wait:
            for t in self._workers:t.join()
