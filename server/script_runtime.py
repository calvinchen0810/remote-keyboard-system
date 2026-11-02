import copy
import time
import uuid
from typing import Optional


class ScriptRuntime:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._last_failure: Optional[dict] = None
        self.active_id: Optional[str] = None
        self.revision = 0
        self.session_id = uuid.uuid4().hex

    def enqueue(self, *, name, steps, loop, condition_ids) -> str:
        job_id = uuid.uuid4().hex
        items = []
        for step in steps:
            params = self._sanitize_params(step)
            item = {"params": params, "state": "pending", "result": None, "error": None}
            children = self._control_children(params)
            if children is not None:
                item["control"] = {
                    "branch": None,
                    "condition_match": None,
                    "iteration": None,
                    "iteration_limit": None,
                }
                item["children"] = {
                    slot: [{"state": "pending", "result": None, "error": None}
                           for _ in child_steps]
                    for slot, child_steps in children.items()
                }
            items.append(item)
        self._jobs[job_id] = {"job_id": job_id, "name": name, "submitted_at": time.time(),
            "phase": "queued", "current_step": None, "iteration": 0, "loop": bool(loop),
            "deadline": None, "error": None, "steps": items,
            "condition_ids": list(dict.fromkeys(condition_ids)),
            "total": sum(self._action_count(item["params"]) for item in items)}
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
            for children in step.get("children", {}).values():
                for child in children:
                    child.update(state="pending", result=None, error=None)
            if "control" in step:
                step["control"].update(branch=None, condition_match=None, iteration=None,
                                       iteration_limit=None)
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

    def set_control_state(self, index, *, branch=None, condition_match=None,
                          iteration=None, iteration_limit=None):
        job = self._jobs.get(self.active_id)
        if job is None or not 0 <= index < len(job["steps"]):
            return
        item = job["steps"][index]
        control = item.get("control")
        if control is None:
            return
        if branch is not None:
            control["branch"] = branch
        if condition_match is not None:
            control["condition_match"] = condition_match
        if iteration is not None:
            control["iteration"] = iteration
            item["state"] = "active"
            if job["error"] == item["error"]:
                job["error"] = next(
                    (step["error"] for step in reversed(job["steps"])
                     if step is not item and step["error"] is not None), None)
            item["error"] = None
            for children in item.get("children", {}).values():
                for child in children:
                    child.update(state="pending", result=None, error=None)
        if iteration_limit is not None:
            control["iteration_limit"] = iteration_limit
        job.update(current_step=index, phase="running", deadline=None)
        self.revision += 1

    def start_child_step(self, index, slot, child_index, phase="running", deadline=None):
        job = self._jobs.get(self.active_id)
        child = self._get_child_item(job, index, slot, child_index)
        if child is None:
            return
        job.update(current_step=index, phase=phase, deadline=deadline)
        job["steps"][index]["state"] = "active"
        child["state"] = "active"
        self.revision += 1

    def complete_child_step(self, index, slot, child_index, result=None, error=None):
        job = self._jobs.get(self.active_id)
        child = self._get_child_item(job, index, slot, child_index)
        if child is None:
            return
        if error is not None:
            message = str(error)
            child.update(state="failed", error=message)
            job["steps"][index].update(state="failed", error=message)
            job["error"] = message
        elif child["state"] != "failed":
            child["state"] = "completed"
        child["result"] = copy.deepcopy(result)
        job.update(phase="running", deadline=None)
        self.revision += 1

    def note_error(self, message, *, job_id=None):
        job = self._jobs.get(self.active_id if job_id is None else job_id)
        if job is None:
            return
        job["error"] = str(message)
        index = job["current_step"]
        if index is not None:
            job["steps"][index].update(state="failed", error=str(message))
        self.revision += 1

    def finish(self, job_id, *, failed=False):
        job = self._jobs.get(job_id)
        if job is not None and failed:
            job["phase"] = "failed"
            self._last_failure = copy.deepcopy(job)
        self._jobs.pop(job_id, None)
        if self.active_id == job_id:
            self.active_id = None
        self.revision += 1

    def clear(self):
        self._jobs.clear()
        self._last_failure = None
        self.active_id = None
        self.revision += 1

    @staticmethod
    def _summary(job):
        return {key: copy.deepcopy(value) for key, value in job.items()
                if key not in {"steps", "condition_ids"}} | {
                    "total": job.get("total", len(job["steps"]))}

    def snapshot(self):
        active = self._jobs.get(self.active_id)
        return {"session_id": self.session_id, "revision": self.revision,
            "active": self._summary(active) if active is not None else None,
            "queued": [self._summary(job) for job_id, job in self._jobs.items() if job_id != self.active_id],
            "last_failure": self._summary(self._last_failure) if self._last_failure is not None else None}

    def detail(self, job_id):
        job = self._jobs.get(job_id)
        if job is None and self._last_failure is not None and self._last_failure["job_id"] == job_id:
            job = self._last_failure
        if job is None:
            return None
        return {**self._summary(job), "steps": copy.deepcopy(job["steps"]),
                "condition_ids": list(job["condition_ids"]), "active": job_id == self.active_id,
                "session_id": self.session_id, "revision": self.revision}

    @staticmethod
    def _sanitize_params(value):
        params = copy.deepcopy(value)

        def strip_templates(node):
            if isinstance(node, dict):
                image_move = node.get("image_move")
                if isinstance(image_move, dict):
                    image_move.pop("template_b64", None)
                for nested in node.values():
                    strip_templates(nested)
            elif isinstance(node, list):
                for nested in node:
                    strip_templates(nested)

        strip_templates(params)
        return params

    @staticmethod
    def _control_children(params):
        if not isinstance(params, dict):
            return None
        if params.get("type") == "if_else":
            return {slot: params.get(slot, []) for slot in ("then_steps", "else_steps")}
        if params.get("type") in {"do_while", "while"}:
            return {"steps": params.get("steps", [])}
        return None

    @classmethod
    def _action_count(cls, params):
        children = cls._control_children(params)
        return 1 if children is None else sum(len(steps) for steps in children.values())

    @staticmethod
    def _get_child_item(job, index, slot, child_index):
        if job is None or not 0 <= index < len(job["steps"]):
            return None
        children = job["steps"][index].get("children", {}).get(slot)
        if children is None or not 0 <= child_index < len(children):
            return None
        return children[child_index]