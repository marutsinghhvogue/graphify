from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()


@scheduler.scheduled_job("cron", hour=2, minute=0)
def rollup_daily():
    """Nightly billing rollup — APScheduler cron trigger."""
    return compute_rollup()


def compute_rollup():
    return {"ok": True}
