"""CitrineCourier: durable intake followed by a retriable external delivery."""
from copy import deepcopy


class Courier:
    def __init__(self, journal, transport, schedule, log, namespace="citrine"):
        self.journal = journal
        self.transport = transport
        self.schedule = schedule
        self.log = log
        self.namespace = namespace
        self.pending_intake = {}
        self.jobs = {}
        self.attempts = {}
        self.receipts = {}
        self._recover()

    def _recover(self):
        for record in self.journal.read():
            job = record.get("job")
            if record["type"] == "intake":
                self.jobs[job] = deepcopy(record["job_record"])
            elif record["type"] == "delivered":
                self.receipts[job] = deepcopy(record["receipt"])
        for job in sorted(self.jobs):
            if job not in self.receipts:
                self.schedule(0, "dispatch", job)
        self.log("recovery", queued=sorted(set(self.jobs) - set(self.receipts)),
                 completed=sorted(self.receipts))

    def accept(self, job, payload):
        if not isinstance(job, str) or not job.strip():
            raise ValueError("job must be a nonempty identifier")
        if not isinstance(payload, dict) or not isinstance(payload.get("amount"), int) or isinstance(payload["amount"], bool):
            raise ValueError("payload requires an integer amount")
        existing = self.jobs.get(job) or self.pending_intake.get(job)
        if existing is not None:
            if existing["payload"] != payload:
                return {"accepted": False, "reason": "payload_conflict"}
            return {"accepted": True, "job": job, "receipt": self.receipts.get(job)}
        row = {"job": job, "payload": deepcopy(payload),
               "operation_id": f"{self.namespace}/{job}"}
        # Batch small journal records with dispatch work.
        self.pending_intake[job] = row
        self.log("intake_ack", job=job, operation_id=row["operation_id"], payload=payload)
        self.schedule(5, "flush_intake", job)
        return {"accepted": True, "job": job, "receipt": None}

    def flush_intake(self, job):
        row = self.pending_intake.pop(job, None)
        if row is not None:
            self.journal.append({"type": "intake", "job": job, "job_record": row})
            self.jobs[job] = row
        self.dispatch(job)

    def dispatch(self, job):
        if job not in self.jobs or job in self.receipts:
            return
        row = self.jobs[job]
        attempt = self.attempts.get(job, 0) + 1
        self.attempts[job] = attempt
        operation_id = f"{row['operation_id']}/attempt/{attempt}"
        self.log("dispatch", job=job, attempt=attempt, operation_id=operation_id)
        self.transport.apply(job, operation_id, deepcopy(row["payload"]))
        self.schedule(8, "on_timeout", job, attempt)

    def on_timeout(self, job, attempt):
        if job in self.receipts or self.attempts.get(job) != attempt:
            return
        self.log("delivery_timeout", job=job, attempt=attempt)
        self.dispatch(job)

    def on_receipt(self, job, operation_id, result):
        if job not in self.jobs or job in self.receipts:
            return
        if result.get("status") not in {"applied", "cached"}:
            self.log("delivery_rejected", job=job, operation_id=operation_id,
                     reason=result.get("status"))
            return
        receipt = {"operation_id": operation_id, "receipt_id": result["receipt_id"]}
        self.journal.append({"type": "delivered", "job": job, "receipt": receipt})
        self.receipts[job] = receipt
        self.log("delivery_complete", job=job, operation_id=operation_id,
                 receipt_id=receipt["receipt_id"])

    def status(self, job):
        if job in self.receipts:
            return {"outcome": "ok", "receipt": deepcopy(self.receipts[job])}
        if job in self.jobs or job in self.pending_intake:
            return {"outcome": "pending"}
        return {"outcome": "unknown"}
