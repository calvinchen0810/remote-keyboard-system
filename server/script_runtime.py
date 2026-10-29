import copy
import time
import uuid
from typing import Optional


class ScriptRuntime:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self.active_id: Optional[str] = None
        self.revision = 0
        self.session_id = uuid.uuid4().hex

    def enqueue(self, *, name, steps, loop, condition_ids) -> str:
        job_id = uuid.uuid4().hex
        items = []
        for step in steps:
            params = copy.deepcopy(step)
            if isinstance(params.get("image_move"), dict):
                params["image_move"].pop("template_b64", None)
            items.append({"params": params, "state": "pending", "result": None, "error": None})
        self._jobs[job_id] = {"job_id": job_id, "name": name, "submitted_at": time.time(),
            "phase": "queued", "current_step": None, "iteration": 0, "loop": bool(loop),
            "deadline": None, "error": None, "steps": items,
            "condition_ids": list(dict.fromkeys(condition_ids))}
        self.revision += 1
        return job_id

    def contains(self, job_id) -> bool:
        return job_id in self._jobs

    def activate(self, job_id):
        if job_id not in self._jobs:
            raise KeyError(job_id)
        self.active_id = job_id
        self._jobs[job_id].update(phase="attaching", started_at=time.time())
        self.revision += 1

    def begin_iteration(self, iteration):
        job = self._jobs.get(self.active_id)
        if job is None:
            return
        job.update(iteration=iteration, phase="running", current_step=None, deadline=None, error=None)
        for step in job["steps"]:
            step.update(state="pending", result=None, error=None)
        self.revision += 1

    def start_step(self, index, phase="running", deadline=None):
        job = self._jobs.get(self.active_id)
        if job is None or not 0 <= index < len(job["steps"]):
            return
        job.update(current_step=index, phase=phase, deadline=deadline)
        job["steps"][index]["state"] = "active"
        self.revision += 1

    def set_phase(self, phase, deadline=None):
        job = self._jobs.get(self.active_id)
        if job is not None:
            job.update(phase=phase, deadline=deadline)
            self.revision += 1

    def complete_step(self, index, result=None):
        job = self._jobs.get(self.active_id)
        if job is None or not 0 <= index < len(job["steps"]):
            return
        step = job["steps"][index]
        if step["state"] != "failed":
            step["state"] = "completed"
        step["result"] = copy.deepcopy(result)
        job.update(phase="running", deadline=None)
        self.revision += 1

    def note_error(self, message):
        job = self._jobs.get(self.active_id)
        if job is None:
            return
        job["error"] = str(message)
        index = job["current_step"]
        if index is not None:
            job["steps"][index].update(state="failed", error=str(message))
        self.revision += 1

    def finish(self, job_id):
        self._jobs.pop(job_id, None)
        if self.active_id == job_id:
            self.active_id = None
        self.revision += 1

    def clear(self):
        self._jobs.clear()
        self.active_id = None
        self.revision += 1

    @staticmethod
    def _summary(job):
        return {key: copy.deepcopy(value) for key, value in job.items()
                if key not in {"steps", "condition_ids"}} | {"total": len(job["steps"])}

    def snapshot(self):
        active = self._jobs.get(self.active_id)
        return {"session_id": self.session_id, "revision": self.revision,
            "active": self._summary(active) if active is not None else None,
            "queued": [self._summary(job) for job_id, job in self._jobs.items() if job_id != self.active_id]}

    def detail(self, job_id):
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {**self._summary(job), "steps": copy.deepcopy(job["steps"]),
                "condition_ids": list(job["condition_ids"]), "active": job_id == self.active_id,
                "session_id": self.session_id, "revision": self.revision}