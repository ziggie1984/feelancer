from __future__ import annotations

import logging
import signal
from datetime import datetime
from typing import TYPE_CHECKING, Callable

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from feelancer.config import FeelancerConfig
from feelancer.data.db import FeelancerDB
from feelancer.lightning.chan_updates import update_channel_policies
from feelancer.lightning.data import LightningCache, LightningSessionCache
from feelancer.lightning.lnd import LNDClient
from feelancer.lightning.models import DBRun
from feelancer.lnd.client import LndGrpc
from feelancer.pid.controller import PidController
from feelancer.pid.data import PidConfig
from feelancer.utils import read_config_file

if TYPE_CHECKING:
    from feelancer.lightning.chan_updates import PolicyProposal
    from feelancer.lightning.client import LightningClient


class TaskRunner:
    def __init__(self, config_file: str):
        self.config_file = config_file
        self.config_dict = read_config_file(self.config_file)
        self.lnclient: LightningClient

        if "lnd" in self.config_dict:
            self.lnclient = LNDClient(LndGrpc.from_file(**self.config_dict["lnd"]))
        else:
            raise ValueError("'lnd' section is not included in config-file")

        if "sqlalchemy" in self.config_dict:
            self.db = FeelancerDB.from_config_dict(
                self.config_dict["sqlalchemy"]["url"]
            )
        else:
            raise ValueError("'sqlalchemy' section is not included in config-file")

        self.pid_controller: PidController | None = None

        """
        Setting up a scheduler which call self._run in an interval of self.seconds.
        """
        config = FeelancerConfig(self.config_dict)
        self.seconds = config.seconds

        scheduler = BlockingScheduler()
        logging.info(f"Running pid every {self.seconds}s")
        self.job = scheduler.add_job(self._run, IntervalTrigger(seconds=self.seconds))

        """
        shutdown_schedule is a callback function which is called when SIGTERM or
        SIGINT signal is received. It shut down the scheduler. 
        """

        def shutdown_scheduler(signum, frame):
            logging.info("Shutdown signal received. Shutting down the scheduler...")
            scheduler.shutdown(wait=True)
            logging.info("Scheduler shutdown completed")

        signal.signal(signal.SIGTERM, shutdown_scheduler)
        signal.signal(signal.SIGINT, shutdown_scheduler)

        logging.info("Scheduler starting...")
        scheduler.start()

    def _update_config_dict(self) -> None:
        """
        Reads the config_dict again from the filesystem. If there is an error
        we will proceed with current dictionary.
        """

        try:
            self.config_dict = read_config_file(self.config_file)
        except Exception as e:
            logging.error("An error occurred during the update of the config: %s", e)

    def _run(self) -> None:
        """
        Running all jobs associated with this task runner. At the moment only pid.
        """

        # Reading the config again from file system to get parameter changes.
        # It serves as a poor man' api. ;-)
        self._update_config_dict()

        ln = LightningCache(self.lnclient)
        config = FeelancerConfig(self.config_dict)
        timestamp_start = datetime.now(pytz.utc)

        store_funcs: list[Callable[[LightningSessionCache], None]] = []
        policy_updates: list[PolicyProposal] = []

        try:
            func, prop = self._run_pid(ln, config, timestamp_start)
            store_funcs.append(func)
            policy_updates += prop
            logging.info("Finished pid controller")

        except Exception as e:
            logging.error("Could not run pid controller")
            logging.exception(e)

        timestamp_end = datetime.now(pytz.utc)

        """Updating the Lightning Backend with new policies."""
        update_channel_policies(self.lnclient, policy_updates, config, timestamp_end)

        """
        Storing the relevant data in the database by calling the store_funcs
        with the cached data.
        We can return early if there is nothing to store.
        """
        if len(store_funcs) == 0:
            return None

        with self.db.session() as db_session:
            try:
                db_run = DBRun(
                    timestamp_start=timestamp_start,
                    timestamp_end=timestamp_end,
                )
                ln_session = LightningSessionCache(ln, db_session, db_run)
                for f in store_funcs:
                    f(ln_session)

                db_session.commit()

                run_time = timestamp_end - timestamp_start
                logging.info(
                    f"Run {db_run.id} successfully finished; start "
                    f"{timestamp_start}; end {timestamp_end}; runtime {run_time}."
                )
            except Exception as e:
                db_session.rollback()
                raise e
            finally:
                db_session.close()

        """
        If config.seconds had changed we modify the trigger of the job.
        """
        if config.seconds != self.seconds:
            self.seconds = config.seconds
            logging.info(f"Interval changed; running pid every {self.seconds}s now")
            self.job.modify(trigger=IntervalTrigger(seconds=self.seconds))

    def _run_pid(
        self, ln: LightningCache, config: FeelancerConfig, timestamp: datetime
    ) -> tuple[Callable[[LightningSessionCache], None], list[PolicyProposal]]:
        pid_config = PidConfig(config.tasks_config["pid"])
        if not self.pid_controller:
            self.pid_controller = PidController(self.db, pid_config, ln.pubkey_local)

        self.pid_controller(pid_config, ln, timestamp)

        func = self.pid_controller.store_data
        prop = self.pid_controller.policy_proposals()
        return func, prop