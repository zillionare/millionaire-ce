"""Lifecycle helpers for application-owned scheduler registrations."""


def remove_owned_jobs(owner, scheduler, job_ids):
    """Remove owned jobs under the service lock and allow the next lifespan to start."""
    with owner._lock:
        for job_id in job_ids:
            scheduler.remove_job(job_id)
        owner._scheduled = False
