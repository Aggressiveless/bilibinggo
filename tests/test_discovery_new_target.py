"""Targets count new BVs across pages, restarts and the persistent BV library."""
import pytest
import time
from contextlib import nullcontext
from fastapi.testclient import TestClient

from src import lottery_discovery as d
from src.lottery_discovery_broad import discover_page
from src.control_store import ControlStore


def bv(index):
    return f"BV{index:010d}"


class PaginatedClient:
    pages = [range(36), range(20, 56), range(56, 92), range(80, 116), range(116, 152)]

    def __init__(self, cache=None, budget=1000):
        self.cache = cache if cache is not None else {}
        self.budget = budget
        self.request_count = 0
        self.calls = []

    def read(self, url, params, **kwargs):
        if self.request_count >= self.budget:
            raise d.DiscoveryStopped("已达到本轮 B 站请求预算")
        self.request_count += 1
        self.calls.append((url, params))
        if url == d.SEARCH_URL:
            return {"code": 0, "data": {"numPages": len(self.pages), "result": [
                {"bvid": bv(index)} for index in self.pages[params["page"] - 1]]}}
        assert url == d.VIDEO_URL
        return {"code": 0, "data": {"owner": {}}}


@pytest.mark.parametrize("comprehensive", [False, True])
def test_100_new_bvs_skip_existing_and_cross_page_duplicates(tmp_path, comprehensive):
    store = ControlStore(tmp_path)
    store.remember_discovery_videos([bv(i) for i in range(20)])
    client = PaginatedClient()
    snapshots = []
    result = discover_page(client, ["抽奖"], target_new_videos=100,
                           known_bvids=store.discovery_video_ids(), comprehensive_page=comprehensive,
                           candidate_limit=0, replay_completed=False,
                           on_video_page=store.remember_discovery_videos, on_progress=snapshots.append)
    assert result["completion_reason"] == "target_reached"
    assert result["videos_checked"] == result["new_videos_checked"] == 100
    assert result["target_new_videos"] == 100 and result["duplicates_skipped"] == 48
    assert not result["has_more"] and result["scan_completed"]
    assert store.discovery_video_ids() == {bv(i) for i in range(120)}
    scanned = [params["bvid"] for url, params in client.calls if url == d.VIDEO_URL]
    assert scanned == [bv(i) for i in range(20, 120)]
    assert snapshots[-1]["new_videos_checked"] == 100
    assert [params["page"] for url, params in client.calls if url == d.SEARCH_URL] == [1, 2, 3, 4, 5]
    assert {params["order"] for url, params in client.calls if url == d.SEARCH_URL} == {"totalrank" if comprehensive else "pubdate"}


@pytest.mark.parametrize("budget", [2, 8])
def test_resume_keeps_claimed_new_bvs_and_page_cursor(tmp_path, budget):
    store = ControlStore(tmp_path)
    store.remember_discovery_videos([bv(i) for i in range(20)])
    first = PaginatedClient(budget=budget)
    partial = discover_page(first, ["抽奖"], target_new_videos=100,
                            known_bvids=store.discovery_video_ids(), candidate_limit=0,
                            on_video_page=store.remember_discovery_videos, replay_completed=False)
    assert partial["completion_reason"] == "request_budget" and partial["has_more"]
    # A fresh client sees the earlier run's BVs in the library, but its own
    # durable cursor must still finish processing and count those claimed BVs.
    second = PaginatedClient(first.cache)
    result = discover_page(second, ["抽奖"], target_new_videos=100,
                           known_bvids=store.discovery_video_ids(), candidate_limit=0,
                           on_video_page=store.remember_discovery_videos, replay_completed=False)
    assert result["resumed"] and result["completion_reason"] == "target_reached"
    assert result["new_videos_checked"] == 100 and store.discovery_video_count() == 120
    scanned = [p["bvid"] for client in [first, second] for url, p in client.calls if url == d.VIDEO_URL]
    assert scanned == [bv(i) for i in range(20, 120)]
    assert result["duplicates_skipped"] == 48


def test_exhaustion_and_later_run_never_recount_old_bvs(tmp_path):
    store = ControlStore(tmp_path)
    store.remember_discovery_videos([bv(i) for i in range(140)])
    for expected in (12, 0):
        result = discover_page(PaginatedClient(), ["抽奖"], target_new_videos=100,
                               known_bvids=store.discovery_video_ids(), candidate_limit=0,
                               on_video_page=store.remember_discovery_videos)
        assert result["new_videos_checked"] == expected
        assert result["completion_reason"] == "search_exhausted" and result["search_exhausted"]
        assert not result["has_more"]
    assert store.discovery_video_count() == 152


def test_repeated_last_page_without_metadata_terminates():
    class Repeating(PaginatedClient):
        def read(self, url, params, **kwargs):
            if url == d.SEARCH_URL:
                self.calls.append((url, params))
                return {"code": 0, "data": {"result": [{"bvid": bv(1)}]}}
            return super().read(url, params, **kwargs)
    client = Repeating()
    result = discover_page(client, ["抽奖"], target_new_videos=100, known_bvids={bv(1)})
    assert result["completion_reason"] == "search_exhausted" and result["new_videos_checked"] == 0
    assert len(client.calls) == 2


def test_saved_batch_survives_loss_of_discovery_client(tmp_path):
    class ProcessStopped(BaseException):
        pass

    store = ControlStore(tmp_path)

    def on_checkpoint(snapshot):
        store.save_discovery_checkpoint("queue-item", snapshot["checkpoint_state"], job_id=1, report={},
                                        checks=snapshot["video_checks"], video_page=snapshot["video_page"])

    class InterruptedClient(PaginatedClient):
        def read(self, url, params, **kwargs):
            if url == d.VIDEO_URL and params["bvid"] == bv(18):
                raise ProcessStopped()
            return super().read(url, params, **kwargs)

    with pytest.raises(ProcessStopped):
        discover_page(InterruptedClient(), ["抽奖"], target_new_videos=24, candidate_limit=0,
                      known_bvids=set(), on_progress=on_checkpoint, checkpoint_batches=True)
    checkpoint = store.discovery_checkpoint("queue-item", max_age=3600)
    assert checkpoint["scan_index"] == 16
    assert store.discovery_video_count() == 24
    client = PaginatedClient()
    result = discover_page(client, ["抽奖"], target_new_videos=24, candidate_limit=0,
                           known_bvids=store.discovery_video_ids(), checkpoint_state=checkpoint,
                           on_progress=on_checkpoint, checkpoint_batches=True, replay_completed=False)
    assert result["resumed"] and result["videos_checked"] == 24
    assert [(url, data["bvid"]) for url, data in client.calls] == [(d.VIDEO_URL, bv(i)) for i in range(16, 24)]


def test_web_screening_keeps_deferred_videos_available_for_review(isolated_home, monkeypatch):
    from web.app import app
    from web.control_service import get_control_service
    from web.job_runner import JobRunner

    target = "1234567890123456789"
    runner = JobRunner()
    service = get_control_service()
    monkeypatch.setattr("web.job_runner.runner", runner)
    monkeypatch.setattr("web.app.runner", runner)
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)

    reads, model_inputs = [], []
    class Client:
        def __init__(self):
            self.cache = {}
            self.request_count = 0
        def read(self, url, params, **kwargs):
            self.request_count += 1
            reads.append((url, params))
            if url == d.SEARCH_URL:
                data = {"numPages": 1, "result": [{"bvid": bv(i)} for i in range(3)]}
            elif url == d.VIDEO_URL:
                index = int(params["bvid"][2:])
                texts = [f"抽奖入口：{d.opus_link(target)}", "今天去散步", f"抽奖入口：{d.opus_link(target)}\n评论区还有说明"]
                data = {"aid": index + 1, "title": texts[index], "owner": {"mid": 42}}
            else:
                assert url == d.REPLIES_URL
                data = {}
            return {"code": 0, "data": data}
        def extract(self, text, **kwargs):
            model_inputs.append(text.strip())
            return {"model_assessment": "not_lottery"}
        def save(self):
            pass

    monkeypatch.setattr("web.control_actions.discovery_client", lambda *a, **kw: nullcontext(Client()))
    checked = []
    def inspect(_client, identifier, **kwargs):
        checked.append(identifier)
        return {"dynamic_id": identifier, "kind": "dynamic", "url": d.opus_link(identifier), "status": "open"}
    monkeypatch.setattr(d, "inspect_dynamic", inspect)

    api = TestClient(app)
    def finish():
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = api.get("/api/task-queue").json()
            if not any(item["state"] in {"queued", "running", "starting"} for item in state["entries"]):
                return api.get("/api/discovery/current").json()
            time.sleep(.03)
        pytest.fail("The discovery queue did not finish")

    initial = api.post("/api/discovery/start?enqueue=true", json={"keywords": ["抽奖"], "target_new_videos": 3})
    assert initial.status_code == 200, initial.text
    first = finish()
    assert first["job"]["state"] == "success" and first["summary"]["eligible"] == 1
    assert first["review_deferred_available"] and first["summary"]["deferred_videos"] == 1
    assert first["summary"]["comment_reads_saved"] == 1
    assert [params["oid"] for url, params in reads if url == d.REPLIES_URL] == [2, 3]
    assert checked == [target] and not model_inputs

    reads.clear()
    review = api.post("/api/discovery/review-deferred", json={"job_id": first["job"]["job_id"]})
    assert review.status_code == 200, review.text
    second = finish()
    assert second["job"]["state"] == "success" and second["summary"]["eligible"] == 1
    assert not second["review_deferred_available"] and second["summary"]["deferred_videos"] == 0
    assert checked == [target] and not reads and model_inputs == ["今天去散步"]


@pytest.mark.parametrize("existing,checked,reason", [(20, 100, "target_reached"), (140, 12, "search_exhausted")])
def test_browser_queue_reports_actual_target_progress(isolated_home, monkeypatch, existing, checked, reason):
    from web.app import app
    from web.control_service import get_control_service
    from web.job_runner import JobRunner
    runner = JobRunner()
    monkeypatch.setattr("web.job_runner.runner", runner)
    monkeypatch.setattr("web.app.runner", runner)
    service = get_control_service()
    monkeypatch.setattr(service, "account", lambda: {"credentials_present": True})
    monkeypatch.setattr("src.llm_settings.is_llm_ready", lambda: True)
    client = PaginatedClient()
    client.save = lambda: None
    monkeypatch.setattr("web.control_actions.discovery_client", lambda *a, **kw: nullcontext(client))
    service.store.remember_discovery_videos([bv(i) for i in range(existing)])
    api = TestClient(app)
    response = api.post("/api/discovery/start?enqueue=true", json={
        "keywords": ["抽奖", "GTA6，PS5"], "keyword_match": "combined", "target_new_videos": 100,
    })
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 8
    while runner.is_running() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not runner.is_running()
    job = api.get("/api/jobs/current").json()
    assert job["state"] == "success"
    assert job["progress_step"] == checked and job["progress_total"] == 100
    assert job["result"]["completion_reason"] == reason
    status = api.get("/api/discovery/current").json()
    assert status["keywords"] == ["抽奖 GTA6 PS5"] and status["keyword_match"] == "combined"
    assert {params["keyword"] for url, params in client.calls if url == d.SEARCH_URL} == {"抽奖 GTA6 PS5"}
    assert status["target_new_videos"] == 100
    assert status["summary"]["new_videos_checked"] == checked
    if checked < 100:
        assert "未达到" in job["message"] and "检索完成" not in job["message"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        entries = api.get("/api/task-queue").json()["entries"]
        if all(row["state"] not in {"queued", "starting", "running"} for row in entries):
            break
        time.sleep(.02)
    assert entries[-1]["state"] == "success"
