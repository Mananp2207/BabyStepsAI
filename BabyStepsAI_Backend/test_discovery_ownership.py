"""Cross-connection and cross-process tests for SQLite discovery leases."""
import multiprocessing
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import job_search_agent as jobs


def _process_claim(path,barrier,queue,run_id):
    jobs.DB=Path(path)
    barrier.wait()
    try:
        jobs._acquire_discovery_run("private prompt omitted",run_id)
        queue.put(("claimed",run_id))
    except HTTPException as error:
        queue.put(("rejected",error.detail.split(":",1)[0]))


class DiscoveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old_db=jobs.DB
        jobs.DB=Path(self.temp.name)/"leases.sqlite3"
        jobs.initialize()

    def tearDown(self):
        jobs.DB=self.old_db
        self.temp.cleanup()

    def test_two_connections_atomically_claim_one_user_slot(self):
        gate=threading.Barrier(2);results=[]
        def claim(run_id):
            gate.wait()
            try:jobs._acquire_discovery_run("prompt",run_id);results.append(("claimed",run_id))
            except HTTPException as error:results.append(("rejected",error.detail.split(":",1)[0]))
        threads=[threading.Thread(target=claim,args=(run_id,)) for run_id in ("run-a","run-b")]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result[0]=="claimed" for result in results),1)
        self.assertEqual(sum(result[0]=="rejected" for result in results),1)
        with jobs.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_discovery_runs WHERE status='running'").fetchone()[0],1)

    def test_two_processes_cannot_claim_different_run_ids(self):
        ctx=multiprocessing.get_context("fork")
        gate=ctx.Barrier(2);queue=ctx.Queue()
        processes=[ctx.Process(target=_process_claim,args=(str(jobs.DB),gate,queue,run_id))
                   for run_id in ("process-a","process-b")]
        for process in processes:process.start()
        results=[queue.get(timeout=10) for _ in processes]
        for process in processes:process.join(10)
        self.assertTrue(all(process.exitcode==0 for process in processes))
        self.assertEqual(sum(result[0]=="claimed" for result in results),1)
        self.assertEqual(sum(result[0]=="rejected" for result in results),1)

    def test_duplicate_run_id_is_not_reused_while_owner_is_active(self):
        jobs._acquire_discovery_run("prompt","same-run")
        with self.assertRaises(HTTPException) as caught:
            jobs._acquire_discovery_run("prompt","same-run")
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("active_worker",caught.exception.detail)
        with jobs.database() as db:self.assertEqual(db.execute("SELECT count(*) FROM job_discovery_runs").fetchone()[0],1)

    def test_workflow_to_run_association_is_stable_across_resume(self):
        jobs._acquire_discovery_run("prompt","stable-run","workflow-one")
        with jobs.database() as db:db.execute("UPDATE job_discovery_runs SET status='interrupted' WHERE run_id='stable-run'")
        with self.assertRaises(HTTPException) as caught:
            jobs._acquire_discovery_run("prompt","stable-run","workflow-two")
        self.assertIn("inconsistent_association",caught.exception.detail)

    def test_dead_expired_lease_is_interrupted_without_losing_checkpoint(self):
        token,_=jobs._acquire_discovery_run("prompt","expired-run")
        old=(jobs.datetime.now(jobs.timezone.utc)-timedelta(minutes=5)).isoformat()
        with jobs.database() as db:
            db.execute("UPDATE job_discovery_runs SET sources_checked=5,postings_retrieved=185,lease_expires_at=?,owner_pid=99999999,owner_host=? WHERE run_id=?",
                       (old,jobs.socket.gethostname(),"expired-run"))
        jobs._acquire_discovery_run("new prompt","next-run")
        with jobs.database() as db:
            oldrow=db.execute("SELECT status,sources_checked,postings_retrieved,error FROM job_discovery_runs WHERE run_id='expired-run'").fetchone()
        self.assertEqual(tuple(oldrow),("interrupted",5,185,"worker_lease_expired"))

    def test_same_run_resumes_after_expired_dead_owner(self):
        jobs._acquire_discovery_run("prompt","resume-expired","wf-expired")
        old=(jobs.datetime.now(jobs.timezone.utc)-timedelta(minutes=5)).isoformat()
        with jobs.database() as db:
            db.execute("UPDATE job_discovery_runs SET sources_checked=3,postings_retrieved=41,lease_expires_at=?,owner_pid=99999999,owner_host=? WHERE run_id=?",
                       (old,jobs.socket.gethostname(),"resume-expired"))
        token,prior=jobs._acquire_discovery_run("prompt","resume-expired","wf-expired")
        self.assertTrue(token)
        with jobs.database() as db:
            row=db.execute("SELECT status,sources_checked,postings_retrieved,owner_token FROM job_discovery_runs WHERE run_id='resume-expired'").fetchone()
        self.assertEqual(tuple(row[:3]),("running",3,41))
        self.assertEqual(row["owner_token"],token)

    def test_restart_reconciles_expired_discovery_and_workflow_together(self):
        old=(jobs.datetime.now(jobs.timezone.utc)-timedelta(minutes=5)).isoformat()
        with jobs.database() as db:
            db.execute("""CREATE TABLE user_workflows(workflow_id TEXT PRIMARY KEY,user_id TEXT,status TEXT,
                current_stage TEXT,pause_requested INTEGER,diagnostics_json TEXT,updated_at TEXT)""")
            db.execute("INSERT INTO user_workflows VALUES('wf-restart',?,'running','discovering',0,'{}',?)",(jobs.USER,old))
            db.execute("""INSERT INTO job_discovery_runs(run_id,user_id,prompt,status,sources_checked,postings_retrieved,
                workflow_id,owner_pid,owner_host,heartbeat_at,lease_expires_at,created_at,updated_at)
                VALUES('restart-run',?,'redacted','running',5,185,'wf-restart',99999999,?,?,?,?,?)""",
                (jobs.USER,jobs.socket.gethostname(),old,old,old,old))
        self.assertIsNone(jobs.active_discovery())
        with jobs.database() as db:
            run=db.execute("SELECT status,sources_checked,postings_retrieved FROM job_discovery_runs WHERE run_id='restart-run'").fetchone()
            wf=db.execute("SELECT status,current_stage,pause_requested,diagnostics_json FROM user_workflows WHERE workflow_id='wf-restart'").fetchone()
        self.assertEqual(tuple(run),("interrupted",5,185))
        self.assertEqual(tuple(wf[:3]),("paused","discovering",1))
        self.assertIn("recovery",wf["diagnostics_json"])

    def test_legacy_running_run_requires_explicit_aged_recovery(self):
        stamp=(jobs.datetime.now(jobs.timezone.utc)-timedelta(minutes=4)).isoformat()
        with jobs.database() as db:
            db.execute("INSERT INTO job_discovery_runs(run_id,user_id,prompt,status,sources_checked,postings_retrieved,created_at,updated_at) VALUES('legacy',?,'redacted','running',5,185,?,?)",
                       (jobs.USER,stamp,stamp))
        with self.assertRaises(HTTPException) as caught:
            jobs.recover_abandoned_discovery("legacy",False)
        self.assertEqual(caught.exception.status_code,422)
        recovered=jobs.recover_abandoned_discovery("legacy",True)
        self.assertTrue(recovered["recovered"])
        jobs._acquire_discovery_run("new prompt","after-recovery")
        with jobs.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_discovery_runs").fetchone()[0],2)
            oldrow=db.execute("SELECT status,sources_checked,postings_retrieved FROM job_discovery_runs WHERE run_id='legacy'").fetchone()
        self.assertEqual(tuple(oldrow),("interrupted",5,185))

    def test_recovery_refuses_a_live_lease(self):
        jobs._acquire_discovery_run("prompt","live-run")
        with self.assertRaises(HTTPException) as caught:
            jobs.recover_abandoned_discovery("live-run",True)
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("active_worker",caught.exception.detail)

    def test_lease_renewal_is_owner_token_guarded(self):
        token,_=jobs._acquire_discovery_run("prompt","owned")
        with jobs.database() as db:
            db.execute("UPDATE job_discovery_runs SET owner_token='different' WHERE run_id='owned'")
        stop=threading.Event()
        with patch.object(jobs,"DISCOVERY_HEARTBEAT_SECONDS",.01):
            worker=threading.Thread(target=jobs._heartbeat_discovery,args=("owned",token,stop))
            worker.start();worker.join(.5)
        with jobs.database() as db:
            row=db.execute("SELECT owner_token,status FROM job_discovery_runs WHERE run_id='owned'").fetchone()
        self.assertEqual(tuple(row),("different","running"))


if __name__=="__main__":unittest.main()
