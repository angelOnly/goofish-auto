import unittest

try:
    from resource_pipeline.goofish_tasks import create_from_specs
except ImportError:
    from goofish_tasks import create_from_specs


class FakeClient:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.generated = []
        self.started = []
        self.job_reads = 0

    def list_tasks(self):
        return self.existing

    def generate(self, payload):
        self.generated.append(payload)
        if payload["decision_mode"] == "ai":
            return {"job": {"job_id": "job-1"}}
        return {"task": {"id": 20}}

    def generation_job(self, job_id):
        self.job_reads += 1
        return {"status": "completed", "task": {"id": 10}}

    def wait_for_generation(self, job_id):
        return self.generation_job(job_id)

    def start(self, task_id):
        self.started.append(task_id)
        return {"ok": True}


class GoofishTaskTests(unittest.TestCase):
    def test_dry_run_does_not_call_api(self):
        client = FakeClient()
        specs = [{"task_name": "AI", "keyword": "AI", "decision_mode": "ai"}]
        result = create_from_specs(client, specs, dry_run=True, start_created=True)
        self.assertEqual(result[0]["status"], "dry_run")
        self.assertEqual(client.generated, [])
        self.assertEqual(client.started, [])

    def test_ai_task_generation_and_start(self):
        client = FakeClient()
        specs = [{"task_name": "AI", "keyword": "AI", "decision_mode": "ai"}]
        result = create_from_specs(client, specs, start_created=True)
        self.assertEqual(result[0]["status"], "created_started")
        self.assertEqual(result[0]["task_id"], 10)
        self.assertEqual(client.started, [10])
        self.assertEqual(client.generated[0]["analyze_images"], True)

    def test_existing_task_is_skipped(self):
        client = FakeClient(existing=[{"id": 99, "name": "AI"}])
        specs = [{"task_name": "AI", "keyword": "AI", "decision_mode": "keyword"}]
        result = create_from_specs(client, specs, start_created=True)
        self.assertEqual(result[0]["status"], "skipped_existing")
        self.assertEqual(client.generated, [])


if __name__ == "__main__":
    unittest.main()
